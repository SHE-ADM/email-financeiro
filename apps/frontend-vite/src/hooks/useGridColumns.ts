import { createElement, type ReactNode, type MouseEvent } from 'react';
import { CheckCircle2, Pencil, Trash2 } from 'lucide-react';
import type {
  FinancialAccountControl,
  EmailControl,
  Supplier,
  CostCenter,
  Bank,
  FinancialAccount,
  ChartAccount,
  ChartAccountGroup,
  ChartAccountSubgroup,
} from '@sheild/shared';
import { STATUS_IDS, STATUS_NAME_BY_ID } from '@sheild/shared';
import StatusBadge from '../components/StatusBadge';
import CheckToggle from '../components/atoms/CheckToggle';
import StatusSelectCell, { type StatusOption } from '../components/atoms/StatusSelectCell';
import type { FinancialAccountFlag, ExpenseDetailRow } from '../services/supabase';
// Formatters de exibição: fonte única em src/lib/format.ts (compartilhada com Consulta.tsx/Emails.tsx).
import { fmtDate, fmtDateTime, fmtMoney, fmtCnpj, fmtCpf, fmtChartAccountFull, fmtSupplierName } from '../lib/format';

// Opções do dropdown inline de situação — value = status_id (fonte única), label =
// nome capitalizado. Ordem de CICLO DE VIDA (a vencer → vencido → …), não a ordem do id
// (a dimensão tem vencido=2 antes de a vencer=3); a ordem de negócio é mais previsível no
// dropdown. Constante de módulo (sem dependência de fetch/timing). 'cartório' com acento.
const STATUS_LIFECYCLE_ORDER = [
  'pendente', 'a vencer', 'vencido', 'prorrogado', 'baixado',
  'protestado', 'cartório', 'pago', 'cancelado', 'falha',
] as const;
export const STATUS_OPTIONS: readonly StatusOption[] = STATUS_LIFECYCLE_ORDER.map((name) => ({
  value: STATUS_IDS[name],
  label: name.charAt(0).toUpperCase() + name.slice(1),
}));

// Rótulo da coluna "Extração" para contas criadas MANUALMENTE pelo usuário (via
// ContaForm/POST /api/contas): não passam pelo pipeline, então extraction_source é
// nulo. O pipeline SEMPRE grava um extraction_source (pdf_text/pdf_vision/image_vision/
// email_body/falha), logo o valor vazio identifica de forma confiável a criação manual.
const MANUAL_EXTRACTION_LABEL = 'Criado pelo usuário';

/** Callback acionado ao marcar/desmarcar um checkbox de flag na célula do grid. */
export type ToggleFlag = (
  row: FinancialAccountControl,
  field: FinancialAccountFlag,
  value: boolean,
) => void;

/** Callback acionado ao alterar a situação de uma conta no dropdown inline (por status_id). */
export type StatusChangeCallback = (rowId: number, newStatusId: number) => Promise<void>;

/** Metadata de uma coluna do grid — fonte única para cabeçalho, render e responsividade. */
export interface ColumnDef<T> {
  // Identidade da coluna (React key + id de preferências de layout). Geralmente um
  // campo de T, mas aceita string sintética para colunas derivadas de JOIN
  // (ex.: 'supplier_name'/'supplier_cnpj' vêm do recurso embutido `supplier`,
  // não são mais colunas próprias — migrations 040/041). `(string & {})` preserva
  // o autocomplete das chaves reais sem travar as sintéticas.
  key: keyof T | (string & {});
  header: string;
  /** Campo enviado ao Supabase para ordenação (mapeia ao SORT_COLS de Consulta). */
  sortKey?: string;
  render: (row: T) => ReactNode;
  align?: 'left' | 'right' | 'center';
  /** Breakpoints em que a coluna some da linha principal (sm=mobile, md=tablet). */
  hideOn?: Array<'sm' | 'md'>;
  /** Se true, ao ficar oculta a coluna desce para a linha de detalhe (segunda linha). */
  secondLine?: boolean;
  /** Rótulo exibido ao lado do valor na linha secundária. */
  secondLineLabel?: string;
  /** Trunca texto longo na célula (com `title`) — evita estourar a largura no mobile. */
  truncate?: boolean;
  /** Quebra o texto em várias linhas (word-wrap) em vez de truncar — colunas largas. */
  wrap?: boolean;
  className?: string;
  /** Largura inicial (px) da coluna no layout gerenciável (resize/pin). Default 160. */
  size?: number;
  /** Largura mínima (px) no resize — evita comprimir números/datas a ponto de ilegíveis. */
  minSize?: number;
}

/**
 * Definição de todas as colunas do grid de /consulta, na ordem de exibição.
 * É uma **factory** (não constante) porque as colunas "Tem NF", "Tem Boleto" e
 * "Situação" renderizam células interativas que escrevem no banco — precisam dos
 * callbacks fornecidos pela página (que fazem o update otimista + persistência REST).
 */
export function getConsultaColumns(
  onToggleFlag: ToggleFlag,
  onStatusChange: StatusChangeCallback,
): ColumnDef<FinancialAccountControl>[] {
  return [
  {
    key: 'invoice_number',
    header: 'Nº Documento',
    size: 130,
    minSize: 110,
    sortKey: 'invoice_number',
    hideOn: ['sm'],
    render: (r) => r.invoice_number ?? '—',
  },
  {
    key: 'issue_date',
    header: 'Emissão',
    size: 100,
    minSize: 90,
    sortKey: 'issue_date',
    hideOn: ['sm', 'md'],
    render: (r) => fmtDate(r.issue_date),
  },
  {
    // Fornecedor vem do JOIN com `supplier` (migrations 040/041). Exibe o nome fantasia e,
    // QUANDO DIVERGE dele, também a razão social (fmtSupplierName) — com fantasia cadastrado
    // como MARCA ("PEGAMIL" para "ITW PPF BRASIL ADESIVOS"), o fantasia sozinho deixava o
    // fornecedor irreconhecível. Ordenação SERVER-SIDE segue por `supplier(trade_name)`: é
    // pelo fantasia que a célula COMEÇA, então a ordem continua casando o que se lê.
    // Texto longo QUEBRA em várias linhas (wrap).
    key: 'supplier_name',
    header: 'Fornecedor',
    size: 170,
    minSize: 130,
    sortKey: 'supplier(trade_name)',
    wrap: true,
    render: (r) => fmtSupplierName(r.supplier),
  },
  {
    // Empresa PAGADORA (JOIN com `company` pela FK sk_company — migrations 083/084):
    // OTIMOTEX TECIDOS (1), LEBIANCO (2), OTIMOTEX FARDOS (3) ou LE BLANC (4). Vem da regra
    // de precedência da extração (read_emails.resolve_sk_company) e do select do
    // ContaForm no cadastro manual. LOGO APÓS o Fornecedor (posição pedida pelo usuário)
    // — são coisas distintas: pode haver conta da LEBIANCO cujo fornecedor é a OTIMOTEX.
    // Ordenação server-side pelo embed `company(trade_name)`, padrão de `supplier(trade_name)`.
    key: 'company',
    header: 'Empresa',
    size: 110,
    minSize: 90,
    sortKey: 'company(trade_name)',
    render: (r) => r.company?.trade_name ?? '—',
  },
  {
    key: 'document_type',
    header: 'Tipo Documento',
    size: 100,
    minSize: 90,
    sortKey: 'document_type',
    hideOn: ['sm', 'md'],
    secondLine: true,
    secondLineLabel: 'Tipo',
    render: (r) => r.document_type ?? '—',
  },
  {
    key: 'payment_method',
    header: 'Tipo Pagamento',
    size: 110,
    minSize: 90,
    sortKey: 'payment_method',
    hideOn: ['sm', 'md'],
    secondLine: true,
    secondLineLabel: 'Pgto',
    render: (r) => r.payment_method ?? '—',
  },
  {
    // Classificação contábil — visualização ENRIQUECIDA: a célula concatena
    // plano de contas + grupo + subgrupo + centro de custo (fmtChartAccountFull).
    // A antiga coluna "Centro de custo" foi removida (dobrada aqui); a edição
    // (inclusão/alteração) do centro e do plano permanece inalterada no ContaForm.
    // Ordenação SERVER-SIDE pela descrição do plano (embed `chart_account`) via a
    // sintaxe do PostgREST `alias(coluna)` — o alias é o mesmo do SELECT_WITH_EMBEDS.
    // Texto longo QUEBRA em várias linhas (wrap) em vez de truncar.
    key: 'chart_account',
    header: 'Plano de contas',
    size: 300,
    minSize: 160,
    sortKey: 'chart_account(account_description)',
    hideOn: ['sm', 'md'],
    secondLine: true,
    secondLineLabel: 'Plano',
    wrap: true,
    render: fmtChartAccountFull,
  },
  {
    key: 'due_date',
    header: 'Vencimento',
    size: 110,
    minSize: 90,
    sortKey: 'due_date',
    render: (r) => fmtDate(r.due_date),
  },
  {
    key: 'amount',
    header: 'Valor',
    size: 120,
    minSize: 90,
    sortKey: 'amount',
    align: 'right',
    render: (r) => fmtMoney(r.amount),
  },
  {
    key: 'has_invoice',
    header: 'NF',
    size: 56,
    minSize: 48,
    align: 'center',
    render: (r) =>
      createElement(CheckToggle, {
        checked: r.has_invoice,
        ariaLabel: `Tem NF — ${r.supplier?.trade_name ?? r.supplier?.legal_name ?? 'registro'}`,
        onToggle: (v: boolean) => onToggleFlag(r, 'has_invoice', v),
      }),
  },
  {
    key: 'has_bank_slip',
    header: 'BOL',
    size: 56,
    minSize: 48,
    align: 'center',
    render: (r) =>
      createElement(CheckToggle, {
        checked: r.has_bank_slip,
        ariaLabel: `Tem Boleto — ${r.supplier?.trade_name ?? r.supplier?.legal_name ?? 'registro'}`,
        onToggle: (v: boolean) => onToggleFlag(r, 'has_bank_slip', v),
      }),
  },
  {
    key: 'status',
    header: 'Situação',
    size: 148,
    minSize: 120,
    // Ordenação de "Situação" é ALFABÉTICA pelo nome. Decisão de negócio: o ciclo de vida
    // não é estritamente linear — de "a vencer" pode-se ir direto para "cancelado"/"falha" —,
    // então a ordem por `status_id` não representa uma sequência real e a alfabética é mais
    // previsível. sortKey='status' é mapeado no serviço para `order=status_dim(status_name)`
    // (a dimensão embutida), pois status_id é a fonte única (coluna texto em remoção).
    sortKey: 'status',
    render: (r) =>
      createElement(StatusSelectCell, {
        rowId: r.id,
        value: r.status_id,
        options: STATUS_OPTIONS,
        onSave: onStatusChange,
      }),
  },
  {
    key: 'extraction_source',
    header: 'Extração',
    size: 150,
    minSize: 130,
    sortKey: 'extraction_source',
    hideOn: ['sm', 'md'],
    // Conta manual (extraction_source nulo) → badge neutro "Criado pelo usuário";
    // conta do pipeline → badge da origem (pdf anexado / imagem anexada / corpo email…).
    render: (r) =>
      createElement(StatusBadge, { value: r.extraction_source || MANUAL_EXTRACTION_LABEL }),
  },
  ];
}

/**
 * Colunas do grid de /emails. É uma **factory** (não constante) porque o "Nº
 * Documento" não vem da linha `EmailControl`: é resolvido por `message_id` no
 * `invoiceMap` (estado carregado pela página). As colunas não têm `sortKey` — o
 * grid de e-mails não ordena por cabeçalho (mantém o comportamento atual).
 */
export function getEmailColumns(invoiceMap: Record<string, string>): ColumnDef<EmailControl>[] {
  return [
    {
      key: 'message_id',
      header: 'Nº Documento',
      size: 150,
      hideOn: ['sm'],
      render: (r) => invoiceMap[r.message_id ?? ''] || '—',
    },
    {
      key: 'received_at',
      header: 'Recebido',
      size: 160,
      hideOn: ['sm', 'md'],
      secondLine: true,
      secondLineLabel: 'Recebido',
      render: (r) => fmtDateTime(r.received_at),
    },
    {
      key: 'sender_email',
      header: 'Remetente',
      size: 220,
      truncate: true,
      render: (r) => r.sender_name || r.sender_email || '—',
    },
    {
      key: 'subject',
      header: 'Assunto',
      size: 300,
      truncate: true,
      render: (r) => r.subject ?? '—',
    },
    {
      key: 'keyword_matched',
      header: 'Tipo documento',
      size: 150,
      hideOn: ['sm', 'md'],
      secondLine: true,
      secondLineLabel: 'Tipo documento',
      render: (r) => createElement(StatusBadge, { value: r.keyword_matched }),
    },
    {
      key: 'has_attachment',
      header: 'PDF',
      size: 64,
      align: 'center',
      hideOn: ['sm', 'md'],
      render: (r) => (r.has_attachment ? '✓' : '—'),
    },
    {
      key: 'pdf_extracted',
      header: 'Extração',
      size: 120,
      hideOn: ['sm', 'md'],
      render: (r) => (r.pdf_extracted ? createElement(StatusBadge, { value: 'extracted' }) : '—'),
    },
    {
      key: 'status',
      header: 'Status',
      size: 150,
      // E-mail 'falha' já revisado (card de detalhes aberto) ganha um check verde
      // ao lado do badge — sinaliza visualmente o que o usuário já triou.
      render: (r) => {
        const badge = createElement(StatusBadge, { value: r.status });
        if (r.status !== 'falha' || !r.reviewed_at) return badge;
        return createElement(
          'span',
          {
            className: 'inline-flex items-center gap-1',
            title: `Revisado em ${fmtDateTime(r.reviewed_at)}`,
          },
          badge,
          createElement(CheckCircle2, {
            size: 14,
            className: 'text-status-success-fg shrink-0',
            'aria-label': 'Revisado',
          }),
        );
      },
    },
  ];
}

/** Callback acionado pelos botões de ação (editar/excluir) da linha de fornecedor. */
type SupplierRowAction = (supplier: Supplier) => void;

// Botão de ação na célula — para o clique (stopPropagation) não disparar o clique da
// linha (que abre a edição). Cada um leva aria-label descritivo com o nome do fornecedor.
function actionButton(
  icon: typeof Pencil,
  label: string,
  className: string,
  onClick: () => void,
  key?: string,
): ReactNode {
  return createElement(
    'button',
    {
      key,
      type: 'button',
      'aria-label': label,
      title: label,
      className,
      onClick: (e: MouseEvent) => {
        e.stopPropagation();
        onClick();
      },
    },
    createElement(icon, { size: 15 }),
  );
}

const supplierLabel = (s: Supplier): string => s.trade_name ?? s.legal_name ?? `#${s.sk_supplier}`;

// Documento do fornecedor numa única coluna: CNPJ quando houver, senão CPF, senão '—'.
const supplierDoc = (s: Supplier): string => {
  if (s.cnpj) return fmtCnpj(s.cnpj);
  if (s.cpf) return fmtCpf(s.cpf);
  return '—';
};

/** Callbacks de ação (editar/excluir) da linha de centro de custo. */
type CostCenterRowAction = (costCenter: CostCenter) => void;

const costCenterLabel = (c: CostCenter): string =>
  c.cost_center_code ?? c.cost_center_description ?? `#${c.cost_center_id}`;

// Classe do botão de ação dos grids de cadastro (fonte única).
const EDIT_BTN_CLS =
  'inline-flex h-7 w-7 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100 hover:text-brand transition-colors';

// Botão de excluir (hard delete) — vermelho semântico (status-error). Ícone-only:
// aria-label descritivo + contraste AA (status-error-fg ≥ 3:1 sobre branco/hover).
const DELETE_BTN_CLS =
  'inline-flex h-7 w-7 items-center justify-center rounded-md text-status-error-fg hover:bg-status-error-bg transition-colors';

// Célula "Ações" dos CRUDs de cadastro: editar sempre; excluir (hard delete) SÓ quando
// `onDelete` é fornecido — as páginas o passam apenas para admin (o hard delete é
// admin-only, imposto de verdade no servidor via requireAdmin).
function actionsCell<T>(
  row: T,
  label: string,
  onEdit: (r: T) => void,
  onDelete?: (r: T) => void,
): ReactNode {
  const buttons: ReactNode[] = [
    actionButton(Pencil, `Editar ${label}`, EDIT_BTN_CLS, () => onEdit(row), 'edit'),
  ];
  if (onDelete) {
    buttons.push(actionButton(Trash2, `Excluir ${label}`, DELETE_BTN_CLS, () => onDelete(row), 'delete'));
  }
  return createElement('div', { className: 'flex items-center justify-center gap-1' }, buttons);
}

/**
 * Colunas do grid de /tabelas/centros-de-custo. É uma **factory** porque a coluna
 * "Ações" renderiza o botão de editar, que depende do callback da página.
 */
export function getCostCenterColumns(
  onEdit: CostCenterRowAction,
  onDelete?: CostCenterRowAction,
): ColumnDef<CostCenter>[] {
  return [
    {
      key: 'cost_center_code',
      header: 'Código',
      sortKey: 'cost_center_code',
      size: 160,
      truncate: true,
      render: (c) => c.cost_center_code ?? '—',
    },
    {
      key: 'cost_center_description',
      header: 'Descrição',
      sortKey: 'cost_center_description',
      size: 360,
      wrap: true,
      render: (c) => c.cost_center_description ?? '—',
    },
    {
      key: '__actions__',
      header: 'Ações',
      size: onDelete ? ACTIONS_COL_SIZE_WITH_DELETE : ACTIONS_COL_SIZE,
      align: 'center',
      render: (c) => actionsCell(c, costCenterLabel(c), onEdit, onDelete),
    },
  ];
}

/**
 * Colunas do card de DETALHE (drill-down) dos gráficos de /dashboard_despesas — enxutas e
 * read-only. NÃO reusa getConsultaColumns/fmtChartAccountFull (tipados ao
 * FinancialAccountControl completo, com curadoria); ExpenseDetailRow só carrega
 * supplier + chart_account + status_id + vencimento/valor. "Plano de conta" mostra só o
 * plano (`código — descrição`), não a hierarquia enriquecida — decisão do usuário.
 * "Situação" é a ÚLTIMA coluna (badge read-only via StatusBadge — sem o dropdown
 * interativo do StatusSelectCell, que exige callback de edição inexistente aqui).
 */
export function getExpenseDetailColumns(): ColumnDef<ExpenseDetailRow>[] {
  return [
    {
      key: 'supplier',
      header: 'Fornecedor',
      size: 200,
      wrap: true,
      // Mesma regra do grid de /consulta (fmtSupplierName): fantasia + razão social
      // quando divergem. Sem isto, o MESMO fornecedor aparecia de formas diferentes
      // conforme a tela — 'PEGAMIL' aqui e 'PEGAMIL · ITW PPF…' lá.
      render: (r) => fmtSupplierName(r.supplier),
    },
    {
      key: 'chart_account',
      header: 'Plano de conta',
      size: 260,
      wrap: true,
      render: (r) =>
        [r.chart_account?.account_code, r.chart_account?.account_description]
          .filter(Boolean).join(' — ') || '—',
    },
    {
      key: 'due_date',
      header: 'Vencimento',
      size: 110,
      render: (r) => fmtDate(r.due_date),
    },
    {
      key: 'amount',
      header: 'Valor',
      align: 'right',
      size: 120,
      render: (r) => fmtMoney(r.amount),
    },
    {
      // Este DataGrid roda sem `enableColumnManagement` (modo não-gerenciável) — `size`
      // é ignorado (cellStyle só aplica width no modo gerenciável) e a tabela é `w-full`
      // sem `table-fixed`, então o navegador quebra o texto da célula por padrão quando
      // falta espaço. `whitespace-nowrap` força o badge a ficar numa linha só; o
      // navegador então dá a esta coluna a largura que ela precisa, encolhendo as
      // colunas com `wrap: true` (Fornecedor/Plano de conta) em vez de quebrar o badge.
      key: 'status_id',
      header: 'Situação',
      size: 130,
      className: 'whitespace-nowrap',
      // Sem fallback local: status_id é NOT NULL com domínio fechado (FK para a dimensão
      // `status`, ids 1-10 — STATUS_NAME_BY_ID é exaustivo), então o lookup nunca deveria
      // vir undefined na prática. Passar o valor cru ao StatusBadge (mesmo padrão de
      // `r.keyword_matched` acima) deixa o PRÓPRIO componente tratar o caso impossível —
      // evita duplicar a lógica de fallback (visto em Consulta.tsx:applyStatusId, que usa
      // String(id) por um motivo distinto: ali o texto vira o status_dim.status_name
      // persistido no update otimista, não só um valor de exibição).
      render: (r) => createElement(StatusBadge, { value: STATUS_NAME_BY_ID[r.status_id] }),
    },
  ];
}

/**
 * Colunas do grid de /fornecedores. É uma **factory** porque a coluna "Ações"
 * renderiza o botão de editar, que depende do callback da página.
 */
export function getSupplierColumns(onEdit: SupplierRowAction): ColumnDef<Supplier>[] {
  return [
    {
      key: 'legal_name',
      header: 'Razão social',
      sortKey: 'legal_name',
      size: 240,
      truncate: true,
      render: (s) => s.legal_name ?? '—',
    },
    {
      key: 'trade_name',
      header: 'Nome fantasia',
      sortKey: 'trade_name',
      size: 200,
      truncate: true,
      render: (s) => s.trade_name ?? '—',
    },
    {
      // CNPJ ou CPF numa única coluna. Ordena por `cnpj` (identificador dominante).
      key: 'cnpj',
      header: 'CNPJ / CPF',
      sortKey: 'cnpj',
      size: 170,
      hideOn: ['sm'],
      secondLine: true,
      secondLineLabel: 'CNPJ / CPF',
      render: supplierDoc,
    },
    {
      key: 'email',
      header: 'E-mail',
      sortKey: 'email',
      size: 220,
      hideOn: ['sm', 'md'],
      truncate: true,
      render: (s) => s.email ?? '—',
    },
    {
      // Classificação contábil DEFAULT do fornecedor — embeds (JOIN) vindos da lista.
      // Ordena pela FK própria (cost_center_id); id 0 = "não informado" → '—'.
      key: 'cost_center',
      header: 'Centro de custo',
      sortKey: 'cost_center_id',
      size: 180,
      wrap: true,
      hideOn: ['sm', 'md'],
      secondLine: true,
      secondLineLabel: 'C. Custo',
      render: (s) =>
        s.cost_center_id
          ? joinCodeDesc(s.cost_center?.cost_center_code, s.cost_center?.cost_center_description)
          : '—',
    },
    {
      key: 'chart_account',
      header: 'Plano de contas',
      sortKey: 'chart_account_id',
      size: 200,
      wrap: true,
      hideOn: ['sm', 'md'],
      secondLine: true,
      secondLineLabel: 'Plano',
      render: (s) =>
        s.chart_account_id
          ? joinCodeDesc(s.chart_account?.account_code, s.chart_account?.account_description)
          : '—',
    },
    {
      key: '__actions__',
      header: 'Ações',
      size: 72,
      align: 'center',
      render: (s) =>
        actionButton(
          Pencil,
          `Editar ${supplierLabel(s)}`,
          'inline-flex h-7 w-7 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100 hover:text-brand transition-colors',
          () => onEdit(s),
        ),
    },
  ];
}

// ── Cadastros do grupo Tabelas (bancos, contas, plano/grupos/subgrupos) ──────────
// Cada factory recebe o callback de editar e um `onDelete` OPCIONAL (só admin) —
// a célula "Ações" mostra editar sempre e excluir só quando onDelete é fornecido.

const ACTIONS_COL_SIZE = 72;
// Mais larga quando há também o botão de excluir (2 botões na célula).
const ACTIONS_COL_SIZE_WITH_DELETE = 104;

/** Junta código + descrição de um embed ("código — descrição"), com fallbacks. */
const joinCodeDesc = (code?: string | null, desc?: string | null): string =>
  [code, desc].filter(Boolean).join(' — ') || '—';

type RowAction<T> = (row: T) => void;

export function getBankColumns(onEdit: RowAction<Bank>, onDelete?: RowAction<Bank>): ColumnDef<Bank>[] {
  const label = (b: Bank): string => b.bank_code ?? b.bank_name ?? `#${b.bank_id}`;
  return [
    { key: 'bank_code', header: 'Código', sortKey: 'bank_code', size: 120, render: (b) => b.bank_code ?? '—' },
    { key: 'bank_name', header: 'Nome', sortKey: 'bank_name', size: 360, wrap: true, render: (b) => b.bank_name ?? '—' },
    {
      key: '__actions__',
      header: 'Ações',
      size: onDelete ? ACTIONS_COL_SIZE_WITH_DELETE : ACTIONS_COL_SIZE,
      align: 'center',
      render: (b) => actionsCell(b, label(b), onEdit, onDelete),
    },
  ];
}

/**
 * Colunas de /tabelas/contas (financial_account). `statusLabel` resolve o nome da
 * situação (status_id → status_name) a partir do lookup carregado pela página.
 */
export function getFinancialAccountColumns(
  statusLabel: (statusId: number) => string,
  onEdit: RowAction<FinancialAccount>,
  onDelete?: RowAction<FinancialAccount>,
): ColumnDef<FinancialAccount>[] {
  const label = (a: FinancialAccount): string => a.account_description ?? `#${a.financial_account_id}`;
  return [
    { key: 'account_description', header: 'Descrição', sortKey: 'account_description', size: 240, wrap: true, render: (a) => a.account_description ?? '—' },
    {
      key: 'bank',
      header: 'Banco',
      size: 220,
      wrap: true,
      render: (a) => joinCodeDesc(a.bank?.bank_code, a.bank?.bank_name),
    },
    { key: 'currency_code', header: 'Moeda', sortKey: 'currency_code', size: 90, render: (a) => a.currency_code ?? '—' },
    { key: 'balance_amount', header: 'Saldo', sortKey: 'balance_amount', size: 120, align: 'right', render: (a) => fmtMoney(a.balance_amount) },
    { key: 'payment_type_id', header: 'Tipo pgto', sortKey: 'payment_type_id', size: 100, align: 'right', render: (a) => String(a.payment_type_id) },
    { key: 'status_id', header: 'Situação', sortKey: 'status_id', size: 130, render: (a) => statusLabel(a.status_id) },
    {
      key: '__actions__',
      header: 'Ações',
      size: onDelete ? ACTIONS_COL_SIZE_WITH_DELETE : ACTIONS_COL_SIZE,
      align: 'center',
      render: (a) => actionsCell(a, label(a), onEdit, onDelete),
    },
  ];
}

export function getChartAccountColumns(
  onEdit: RowAction<ChartAccount>,
  onDelete?: RowAction<ChartAccount>,
): ColumnDef<ChartAccount>[] {
  const label = (c: ChartAccount): string => c.account_code ?? c.account_description ?? `#${c.chart_account_id}`;
  return [
    { key: 'account_code', header: 'Código', sortKey: 'account_code', size: 130, render: (c) => c.account_code ?? '—' },
    { key: 'account_description', header: 'Descrição', sortKey: 'account_description', size: 260, wrap: true, render: (c) => c.account_description ?? '—' },
    {
      key: 'cost_center',
      header: 'Centro de custo',
      // Ordenação SERVER-SIDE alfabética pela descrição do embed via sintaxe do
      // PostgREST `alias(coluna)` — o alias é o mesmo do SELECT_WITH_EMBEDS (igual ao
      // /consulta). A ordem casa o texto exibido.
      sortKey: 'cost_center(cost_center_description)',
      size: 180,
      wrap: true,
      hideOn: ['sm', 'md'],
      render: (c) => (c.cost_center_id ? joinCodeDesc(c.cost_center?.cost_center_code, c.cost_center?.cost_center_description) : '—'),
    },
    {
      key: 'group',
      header: 'Grupo',
      sortKey: 'group(group_description)',
      size: 180,
      wrap: true,
      hideOn: ['sm', 'md'],
      render: (c) => (c.chart_account_group_id ? joinCodeDesc(c.group?.group_code, c.group?.group_description) : '—'),
    },
    {
      key: 'subgroup',
      header: 'Sub Grupo',
      sortKey: 'subgroup(subgroup_description)',
      size: 200,
      wrap: true,
      hideOn: ['sm', 'md'],
      render: (c) => (c.chart_account_subgroup_id ? joinCodeDesc(c.subgroup?.subgroup_code, c.subgroup?.subgroup_description) : '—'),
    },
    {
      key: '__actions__',
      header: 'Ações',
      size: onDelete ? ACTIONS_COL_SIZE_WITH_DELETE : ACTIONS_COL_SIZE,
      align: 'center',
      render: (c) => actionsCell(c, label(c), onEdit, onDelete),
    },
  ];
}

/**
 * Colunas do grid COMPLEMENTAR de /tabelas/centros-de-custo — o plano de contas
 * lançável vinculado ao centro selecionado. Read-only (sem ações) e sem a coluna
 * "Centro de custo" (é o centro selecionado, redundante). Ordenação server-side pela
 * descrição dos embeds grupo/subgrupo (mesma sintaxe `alias(coluna)` do /consulta).
 */
export function getCostCenterChartAccountColumns(): ColumnDef<ChartAccount>[] {
  return [
    { key: 'account_code', header: 'Código', sortKey: 'account_code', size: 130, render: (c) => c.account_code ?? '—' },
    { key: 'account_description', header: 'Descrição', sortKey: 'account_description', size: 280, wrap: true, render: (c) => c.account_description ?? '—' },
    {
      key: 'group',
      header: 'Grupo',
      sortKey: 'group(group_description)',
      size: 200,
      wrap: true,
      render: (c) => (c.chart_account_group_id ? joinCodeDesc(c.group?.group_code, c.group?.group_description) : '—'),
    },
    {
      key: 'subgroup',
      header: 'Sub Grupo',
      sortKey: 'subgroup(subgroup_description)',
      size: 220,
      wrap: true,
      render: (c) => (c.chart_account_subgroup_id ? joinCodeDesc(c.subgroup?.subgroup_code, c.subgroup?.subgroup_description) : '—'),
    },
  ];
}

export function getChartAccountGroupColumns(
  onEdit: RowAction<ChartAccountGroup>,
  onDelete?: RowAction<ChartAccountGroup>,
): ColumnDef<ChartAccountGroup>[] {
  const label = (g: ChartAccountGroup): string => g.group_code ?? g.group_description ?? `#${g.chart_account_group_id}`;
  return [
    { key: 'group_code', header: 'Código', sortKey: 'group_code', size: 120, render: (g) => g.group_code ?? '—' },
    { key: 'group_description', header: 'Descrição', sortKey: 'group_description', size: 320, wrap: true, render: (g) => g.group_description ?? '—' },
    {
      key: 'type_group',
      header: 'Natureza',
      sortKey: 'type_group_id',
      size: 160,
      wrap: true,
      render: (g) => g.type_group?.type_group_description ?? '—',
    },
    {
      key: '__actions__',
      header: 'Ações',
      size: onDelete ? ACTIONS_COL_SIZE_WITH_DELETE : ACTIONS_COL_SIZE,
      align: 'center',
      render: (g) => actionsCell(g, label(g), onEdit, onDelete),
    },
  ];
}

export function getChartAccountSubgroupColumns(
  onEdit: RowAction<ChartAccountSubgroup>,
  onDelete?: RowAction<ChartAccountSubgroup>,
): ColumnDef<ChartAccountSubgroup>[] {
  const label = (s: ChartAccountSubgroup): string =>
    s.subgroup_code ?? s.subgroup_description ?? `#${s.chart_account_subgroup_id}`;
  return [
    { key: 'subgroup_code', header: 'Código', sortKey: 'subgroup_code', size: 130, render: (s) => s.subgroup_code ?? '—' },
    { key: 'subgroup_description', header: 'Descrição', sortKey: 'subgroup_description', size: 280, wrap: true, render: (s) => s.subgroup_description ?? '—' },
    {
      key: 'group',
      header: 'Grupo',
      size: 220,
      wrap: true,
      render: (s) => joinCodeDesc(s.group?.group_code, s.group?.group_description),
    },
    {
      key: 'type_group',
      header: 'Tipo',
      sortKey: 'type_group_id',
      size: 160,
      wrap: true,
      render: (s) => s.type_group?.type_group_description ?? '—',
    },
    {
      key: '__actions__',
      header: 'Ações',
      size: onDelete ? ACTIONS_COL_SIZE_WITH_DELETE : ACTIONS_COL_SIZE,
      align: 'center',
      render: (s) => actionsCell(s, label(s), onEdit, onDelete),
    },
  ];
}
