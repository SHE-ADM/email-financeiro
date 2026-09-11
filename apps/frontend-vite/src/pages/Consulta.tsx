// src/pages/Consulta.tsx
import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
  RefreshCw,
  Download,
  AlertCircle,
  TrendingUp,
  Clock,
  FileText,
  CheckCircle2,
  Search,
  X,
  Pencil,
  Trash2,
  Info,
  type LucideIcon,
} from 'lucide-react';
import {
  DOCUMENT_TYPES,
  PAYMENT_METHODS,
  ACCOUNT_STATUSES,
  STATUS_IDS,
  STATUS_ID_PAGO,
  STATUS_ID_VENCIDO,
  STATUS_ID_A_VENCER,
  STATUS_ID_CANCELADO,
  STATUS_NAME_BY_ID,
} from '@sheild/shared';
import type { FinancialAccountControl, FinancialAccountControlCreate } from '@sheild/shared';
import {
  getFinancialAccountControl,
  getFinancialStats,
  setFinancialAccountFlag,
  setFinancialAccountStatus,
  setFinancialAccountStatusBulk,
  getAppUsers,
  type FinancialStats,
} from '../services/supabase';
import { startEmailRead, getEmailReadProgress, type ReadProgress } from '../services/emailReader';
import { EMAIL_READER_ENABLED } from '../lib/featureFlags';
import { updateConta, deleteConta } from '../services/contas';
import { useAuth } from '../contexts/AuthContext';
import { suspendIdleLogout, resumeIdleLogout } from '../hooks/useIdleLogout';
import { getErrorMessage } from '../lib/getErrorMessage';
import { SENTINEL_AUTHOR_ID, SENTINEL_AUTHOR_EMAIL } from '../lib/sentinelAuthor';
import Alert from '../components/atoms/Alert';
import ExpandableText from '../components/ExpandableText';
import DataGrid from '../components/organisms/DataGrid';
import ContaForm from '../components/organisms/ContaForm';
import ContaAttachments from '../components/organisms/ContaAttachments';
import { uploadContaAttachments, type UploadOutcome } from '../services/contaAttachments';
import { getConsultaColumns, STATUS_OPTIONS, type ToggleFlag, type StatusChangeCallback } from '../hooks/useGridColumns';
import { useCompanyOptions } from '../hooks/useCompanyOptions';
import { useClassificationFilterOptions } from '../hooks/useClassificationFilterOptions';
import ChartAccountSelect from '../components/molecules/ChartAccountSelect';
import { fmtDate, fmtDateTime, fmtMoney, fmtCnpj, fmtCostCenter, fmtChartAccount, fmtSupplierName, isoDaysFromToday } from '../lib/format';
import { nextPaymentDate } from '../lib/paymentDate';
import { csvCell } from '../lib/csv';
import { appendUniqueById } from '../lib/appendUniqueById';

// Fornecedor no card de detalhe: id (sk_supplier) concatenado ao nome com " - ".
const fmtSupplier = (r: FinancialAccountControl): string => {
  // Decide pelo DADO, não pelo texto que o helper devolve: comparar com o literal '—'
  // acoplava este componente ao sentinela de fmtSupplierName, e uma troca lá faria o
  // detalhe exibir "1193 - —" sem nada acusar.
  const s = r.supplier;
  const temNome = Boolean(s?.trade_name?.trim() || s?.legal_name?.trim());
  return temNome ? `${r.sk_supplier} - ${fmtSupplierName(s)}` : String(r.sk_supplier);
};

const PAGE_SIZE = 50;

// Fixação inicial do grid de /consulta: as 3 colunas-chave de contexto à esquerda.
// Constante de módulo (ref estável) — evita recriar o objeto a cada render.
const CONSULTA_DEFAULT_PINNING = { left: ['invoice_number', 'issue_date', 'supplier_name'], right: [] };

// Intervalo [hoje, hoje+7d] em YYYY-MM-DD. Função de MÓDULO (fora do componente) para
// não disparar a regra de pureza do React Compiler — Date.now/new Date são impuros e não
// podem ser chamados no escopo de render do componente.
// Filtro do card "A vencer em 7 dias". Precisa reproduzir EXATAMENTE o predicado que
// produziu o número — `status_id = a vencer` E vencimento na janela (ver
// aggregateFinancialStats). Sem o `statusId`, o clique caía no `BASE_FILTERS.statusId =
// undefined` e o grid voltava com QUALQUER situação não-cancelada: medido em 2026-08-08,
// o card dizia 68 e o clique trazia 69 (uma conta já paga vencendo na janela). A
// divergência cresce com a operação normal — é a contagem de pagas dentro da janela.
//
// Datas em `isoDaysFromToday` (LOCAL): com `toISOString()` (UTC) a janela andava um dia
// das 21h à meia-noite em UTC−3, e o card discordava do grid — ver lib/format.ts.
function next7DaysRange(): { dateFrom: string; dateTo: string; statusId: number } {
  return {
    dateFrom: isoDaysFromToday(0),
    dateTo: isoDaysFromToday(7),
    statusId: STATUS_ID_A_VENCER,
  };
}

// "Atualizar" em /consulta dispara a leitura IMAP dos últimos 7 dias (mesmo motor
// de /emails) — assim o usuário traz e-mails novos sem sair da consulta.
const REFRESH_DAYS = 7;
const PROGRESS_POLL_MS = 1500;
const GRID_REFRESH_EVERY = 5; // a cada ~7,5s recarrega o grid durante o processamento
const PROGRESS_MAX_ERRORS = 20; // ~30s sem contato com o backend → aborta o poll
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

// Colunas do CSV: cada uma define o cabeçalho e como extrair o valor da linha.
// supplier_name/supplier_cnpj vêm do JOIN com `supplier` (não são mais colunas
// da conta — migrations 040/041); as demais saem direto do registro.
type CsvCol = { header: string; get: (r: FinancialAccountControl) => string | number | null | undefined };

const CSV_COLS: CsvCol[] = [
  { header: 'due_date', get: (r) => r.due_date },
  { header: 'status', get: (r) => r.status_dim?.status_name ?? '' },
  { header: 'supplier_name', get: (r) => r.supplier?.trade_name ?? r.supplier?.legal_name },
  { header: 'supplier_legal_name', get: (r) => r.supplier?.legal_name },
  { header: 'supplier_cnpj', get: (r) => r.supplier?.cnpj ?? r.supplier?.cpf },
  { header: 'document_type', get: (r) => r.document_type },
  { header: 'cost_center', get: (r) => fmtCostCenter(r) },
  { header: 'chart_account', get: (r) => fmtChartAccount(r) },
  { header: 'amount', get: (r) => r.amount },
  { header: 'amount_charged', get: (r) => r.amount_charged },
  { header: 'discount', get: (r) => r.discount },
  { header: 'other_deductions', get: (r) => r.other_deductions },
  { header: 'fine_interest', get: (r) => r.fine_interest },
  { header: 'other_additions', get: (r) => r.other_additions },
  { header: 'payment_method', get: (r) => r.payment_method },
  { header: 'nosso_numero', get: (r) => r.nosso_numero },
  { header: 'invoice_number', get: (r) => r.invoice_number },
  { header: 'barcode', get: (r) => r.barcode },
  { header: 'description', get: (r) => r.description },
  { header: 'email_body_excerpt', get: (r) => r.email_body_excerpt },
  { header: 'processing_notes', get: (r) => r.processing_notes },
];

function exportCsv(rows: FinancialAccountControl[]) {
  const header = CSV_COLS.map((c) => c.header).join(';');
  // csvCell escapa aspas, remove quebras de linha internas e NEUTRALIZA injeção de
  // fórmula (= + - @) — conteúdo de e-mail hostil não vira fórmula no Excel/Sheets.
  const body = rows.map((r) => CSV_COLS.map((c) => csvCell(c.get(r))).join(';'));
  const blob = new Blob(['﻿' + [header, ...body].join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `financial_account_control_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
}

// Rótulos de mês — mesmos do Dashboard (princípio de filtro por mês/ano reaproveitado).
const MONTHS = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun', 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez'];
const MONTHS_FULL = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho', 'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro'];

// Janela de coalescência dos filtros: mudanças dentro dela viram UM apply só. Curta o
// bastante para parecer imediata, longa o bastante para agrupar De+Até e a digitação.
const FILTER_APPLY_DELAY_MS = 300;

// Coluna de data do INTERVALO De/Até. O período (botões de mês) tem o seu próprio campo
// e só aceita vencimento/emissão — os dois são independentes de propósito.
type RangeDateField = 'due_date' | 'issue_date' | 'payment_date';

// Rótulo por valor num Record, não num ternário: com três valores, um
// `x === 'due_date' ? 'Vencimento' : 'Emissão'` rotularia `payment_date` como "Emissão"
// — errado, mudo e sem erro de tipo. Assim, um valor novo vira erro de compilação.
const RANGE_DATE_FIELD_LABEL: Record<RangeDateField, string> = {
  due_date: 'Vencimento',
  issue_date: 'Emissão',
  payment_date: 'Pagamento',
};

// Ressalva de DADO, exposta ao usuário no `title` do seletor: `payment_date` é NULL em
// toda conta não paga (então o intervalo por pagamento descarta o que está em aberto —
// combinado com Situação "a vencer" dá 0 linhas, sem erro), e nas contas pagas ANTES da
// migration 096 o backfill gravou o VENCIMENTO. Não há dado real alternativo no banco.
const RANGE_DATE_FIELD_HINT =
  'Data usada no intervalo De/Até (independente do período por mês). ' +
  'Pagamento mostra apenas contas já pagas — as em aberto não têm data de pagamento.';

// Contraparte da ressalva acima, para o seletor do PERÍODO. Ele governa só os botões de
// mês/ano — e enquanto houver intervalo De/Até preenchido, o período não está em vigor (o
// intervalo tem precedência no serviço), então trocar a coluna aqui não muda a consulta na
// hora. O controle NÃO é desabilitado nesse estado, e a diferença é a razão desta ressalva
// existir: clicar num mês LIMPA o intervalo, e é esta coluna que passa a valer no mesmo
// clique. Desabilitá-lo obrigaria a apagar as datas antes de poder escolher a coluna.
const PERIOD_DATE_FIELD_HINT =
  'Coluna usada pelos botões de mês e ano. Com o intervalo De/Até preenchido o período ' +
  'fica suspenso, e esta coluna volta a valer ao escolher um mês (o que limpa o intervalo).';

// Rótulo VISÍVEL do botão de busca é sempre "Buscar" (copy de produto); o nome acessível diz
// o que o clique faz DE FATO — e isso depende do estado, então uma frase única mentiria numa
// das metades. Sem intervalo, o clique alarga o período para todos os meses e anos. COM
// intervalo, o período já está global (defini-lo zera mês/ano) e o intervalo é PRESERVADO:
// anunciar "todos os períodos" ali seria falso, porque a consulta segue restrita às datas
// digitadas. Os dois começam por "Buscar", como exige a WCAG 2.5.3 (Label in Name).
function searchButtonName(hasDateRange: boolean): string {
  return hasDateRange ? 'Buscar mantendo o intervalo de datas' : 'Buscar em todos os meses e anos';
}

interface ConsultaFilters {
  supplier: string;
  docType: string;
  // Situação filtrada por status_id (fonte única). undefined = sem filtro.
  statusId?: number;
  // Empresa pagadora (sk_company: 1=OTIMOTEX TECIDOS, 2=LEBIANCO, 3=OTIMOTEX FARDOS, 4=LE BLANC). undefined = todas.
  skCompany?: number;
  paymentMethod: string;
  // Coluna do filtro de PERÍODO (botões de mês/ano): vencimento (padrão) ou emissão.
  dateField: 'due_date' | 'issue_date';
  // Mês (0-indexed) / ano selecionados. Ambos null = escopo "Todas" (sem filtro de período).
  month: number | null;
  year: number | null;
  // Coluna do INTERVALO explícito De/Até — seletor PRÓPRIO, sem nenhuma ligação com o
  // `dateField` da linha dos meses (pedido do dono do produto). É o único que oferece
  // `payment_date`; ver a ressalva sobre NULL/backfill em RANGE_DATE_FIELD_HINT.
  rangeDateField: RangeDateField;
  // Range explícito — inputs De/Até e o card "A vencer em 7 dias" (este sobre due_date).
  dateFrom: string;
  dateTo: string;
  // ── 2ª linha: classificação contábil ────────────────────────────────────────
  // Independentes entre si (combinados por AND, sem cascata) e aplicados no
  // "Buscar"/Enter, como Empresa/Tipo Documento/Situação.
  // Plano de contas pela DESCRIÇÃO (é o que o ChartAccountSelect devolve); '' = sem
  // filtro, e nenhuma descrição real é string vazia, então não há ambiguidade.
  chartAccountDescription: string;
  chartAccountSubgroupId?: number;
  chartAccountGroupId?: number;
  costCenterId?: number;
}

// Campos não-período zerados (vencimento como campo de data padrão).
const BASE_FILTERS = {
  supplier: '',
  docType: '',
  statusId: undefined as number | undefined,
  // Empresa: undefined = todas. Como os demais filtros, é ZERADO ao clicar num card de KPI
  // (que reseta a view) e no "Limpar" — ambos derivam daqui.
  skCompany: undefined as number | undefined,
  paymentMethod: '',
  dateField: 'due_date' as const,
  // Declarar AQUI (e não só em initialFilters) é o que faz "Limpar" e os cards de KPI
  // resetarem o seletor do intervalo. Em particular, é o que mantém o card "A vencer em
  // 7 dias" filtrando VENCIMENTO mesmo que o usuário tenha deixado "Pagamento"
  // selecionado antes de clicar — o spread de allPeriodFilters() reseta primeiro.
  rangeDateField: 'due_date' as RangeDateField,
  dateFrom: '',
  dateTo: '',
  // Classificação contábil (2ª linha). Estar AQUI é o que faz "Limpar" e os cards de
  // KPI zerarem estes filtros sem nenhuma linha extra: initialFilters(),
  // allPeriodFilters(), handleClear e handleCardFilter derivam todos daqui.
  chartAccountDescription: '',
  chartAccountSubgroupId: undefined as number | undefined,
  chartAccountGroupId: undefined as number | undefined,
  costCenterId: undefined as number | undefined,
};

// Opções do filtro de situação — value = status_id (fonte única), label = nome da
// dimensão (mesmo estilo cru/minúsculo das demais selects de filtro: tipo/pagamento).
const STATUS_FILTER_OPTIONS = ACCOUNT_STATUSES.map((name) => ({
  id: STATUS_IDS[name],
  label: name,
}));

// Mensagem do grid vazio. Precisa dizer em QUE ESCOPO não encontrou — a mensagem antiga
// ("ajuste os filtros e clique em Buscar") passou a mentir duas vezes desde a aplicação
// automática: o filtro JÁ foi aplicado, e "Buscar" agora alarga o período em vez de aplicar.
//
// O caso concreto que expôs isso: escolher um plano de contas e ver o grid vazio parece
// "o filtro não fez nada", quando na verdade o filtro RESTRINGE dentro do mês em tela.
// Medido no cadastro real — "Mercadorias para Revenda" tem 192 contas no total e 60 em
// agosto/2026; já "Cursos Profissionalizantes" existe no cadastro e não tem conta alguma.
// Sem nomear o período, os dois casos são indistinguíveis de um filtro quebrado.
function emptyGridMessage(applied: ConsultaFilters): string {
  if (applied.month != null && applied.year != null) {
    // "todos os meses e anos" repete o nome acessível do próprio botão (`searchButtonName`)
    // — a mensagem que manda clicar e o que o leitor de tela anuncia ao chegar nele têm de
    // descrever a mesma ação, senão a instrução aponta para um botão que "diz" outra coisa.
    return `Nenhum registro em ${MONTHS_FULL[applied.month]}/${applied.year} com estes filtros — use "Buscar" para procurar em todos os meses e anos.`;
  }
  return 'Nenhum registro encontrado com estes filtros.';
}

// Estado padrão de navegação: mês/ano corrente por vencimento (grid abre no mês atual).
// Função de MÓDULO — new Date() é impuro e não pode ser chamado no escopo de render
// (mesma razão de next7DaysRange).
function initialFilters(): ConsultaFilters {
  const d = new Date();
  return { ...BASE_FILTERS, month: d.getMonth(), year: d.getFullYear() };
}

// Base "todos os períodos" para os cards globais (sem filtro de mês/ano).
function allPeriodFilters(): ConsultaFilters {
  return { ...BASE_FILTERS, month: null, year: null };
}

// Update otimista da situação de uma linha: grava status_id (fonte única), sincroniza o
// embed status_dim (nome exibido no badge/CSV/detalhe, resolvido da dimensão pelo id) e
// espelha a trigger `trg_fac_payment_date` via `nextPaymentDate` (lib/paymentDate.ts).
function applyStatusId(r: FinancialAccountControl, id: number): FinancialAccountControl {
  const name = STATUS_NAME_BY_ID[id] ?? String(id);
  return {
    ...r,
    status_id: id,
    status_dim: { status_name: name, status_short_name: name },
    payment_date: nextPaymentDate(r, id),
  };
}

// Situações EM ABERTO — únicas que a baixa automática pode converter para "pago"
// (preserva cancelado/baixado/protestado/cartório/prorrogado e o que já está pago).
const OPEN_STATUS_IDS: readonly number[] = [STATUS_IDS.pendente, STATUS_ID_VENCIDO, STATUS_ID_A_VENCER];

// Data local YYYY-MM-DD (não UTC) — evita "voltar um dia" perto da meia-noite. Função de
// MÓDULO: new Date() é impuro e não pode ser chamado no escopo de render (mesma razão de
// next7DaysRange/initialFilters).
function todayLocalISO(): string {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${mm}-${dd}`;
}

// Regra de baixa automática (espelha o batch diário em Python `baixa-automatica`): conta
// com NF E Boleto marcados, vencimento <= hoje e ainda EM ABERTO é considerada paga.
function qualifiesForAutoPago(r: FinancialAccountControl): boolean {
  if (!r.has_invoice || !r.has_bank_slip) return false;
  if (!r.due_date || r.due_date > todayLocalISO()) return false;
  return OPEN_STATUS_IDS.includes(r.status_id);
}

interface MetricCard {
  icon: LucideIcon;
  label: string;
  value: number;
  fmt: (v: number) => string | number;
  /** Valor monetário exibido abaixo do número principal (null = não exibir). */
  amount?: number | null;
  danger?: boolean;
  success?: boolean;
  /** Tom atenuado — valor/count em cinza médio (text-slate-600), diferenciando do total em preto forte. */
  muted?: boolean;
  cardId?: string;
  onCardClick?: () => void;
}

// Sentinela: default de created_by/updated_by/status_changed_by quando não há usuário real
// resolvido (migrations 076/077; identidade trocada para financeiro@otimotex.com.br pela 110).
// "Última edição por" / "Situação alterada por" apontando p/ ele não representam uma edição de
// um usuário de verdade — então não são exibidos.
//
// ⚠️ Consequência conhecida da identidade atual: `financeiro@otimotex.com.br` é uma conta de
// login REAL. Quando alguém entrar com ela e editar uma conta, esta regra vai ocultar a
// autoria dessa edição, porque o código trata a identidade como "nenhum usuário". Foi decisão
// deliberada (substituição literal do sentinela anterior); a alternativa mapeada é ocultar
// pelo FATO ("nunca editada": `updated_by = created_by` e `status_changed_at = created_at`)
// em vez de pela identidade, o que removeria o acoplamento a um e-mail específico.

export default function Consulta() {
  // Só o GRUPO ADMINISTRADOR vê/executa o hard delete de conta (o backend também impõe
  // via requireAdminGroup — o gate de UI é cosmético). Ver "Hard delete" no CLAUDE.md.
  const { isAdminGroup } = useAuth();
  const [rows, setRows] = useState<FinancialAccountControl[]>([]);
  // KPIs do filtro aplicado — fonte ÚNICA dos 5 cards E do rodapé (contagem + valor).
  const [stats, setStats] = useState<Partial<FinancialStats>>({});
  const [sel, setSel] = useState<FinancialAccountControl | null>(null);
  // Diretório id→e-mail (view app_user) para exibir o AUTOR no detalhe (migration 077).
  const [appUsers, setAppUsers] = useState<Record<string, string>>({});
  const userEmail = (id: string | null): string => (id ? (appUsers[id] ?? id) : '—');
  // Autor é o sentinela? Por UUID (robusto quando o diretório app_user ainda não carregou)
  // OU pelo e-mail resolvido.
  const isSentinelAuthor = (id: string | null): boolean =>
    id === SENTINEL_AUTHOR_ID || userEmail(id) === SENTINEL_AUTHOR_EMAIL;
  // Edição de conta (modal com ContaForm → PATCH /api/contas/:id).
  const [editing, setEditing] = useState<FinancialAccountControl | null>(null);
  const [editSubmitting, setEditSubmitting] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  // Conta salva mas algum anexo falhou — nem erro (a conta foi gravada) nem sucesso limpo.
  const [editWarning, setEditWarning] = useState<string | null>(null);
  const editDialogRef = useRef<HTMLDialogElement>(null);
  // Hard delete (grupo Administrador): confirmação inline no detalhe. `confirmDelete` = id
  // da conta com exclusão armada; `deleting` bloqueia o duplo-clique; `deleteError` inline.
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [f, setF] = useState<ConsultaFilters>(initialFilters);
  const [applied, setApplied] = useState<ConsultaFilters>(initialFilters);
  // Referência do mês/ano corrente (base dos botões de ano quando o escopo é "Todas").
  // Inicializador de useState — fora do escopo de render puro.
  const [nowRef] = useState(() => {
    const d = new Date();
    return { month: d.getMonth(), year: d.getFullYear() };
  });
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [activeCard, setActiveCard] = useState<string | null>(null);
  const [sort, setSort] = useState<{ col: string | null; dir: 'asc' | 'desc' | null }>({ col: null, dir: null });
  // Leitura IMAP disparada pelo botão "Atualizar" (busca dos últimos 7 dias).
  const [reading, setReading] = useState(false);
  const [progress, setProgress] = useState<ReadProgress | null>(null);
  const readingRef = useRef(false);
  // Patch acumulado à espera de virar filtro aplicado. É ESTADO, não ref: o timer passa a
  // ser propriedade de um efeito, cujo cleanup o cancela sozinho — inclusive ao desmontar
  // e quando um caminho síncrono (Buscar/Limpar/card/período) zera o pendente. Com refs,
  // a leitura dentro dos handlers entrava na cadeia `cards → onCardClick` construída no
  // render e caía na regra react-hooks/refs.
  const [pendingApply, setPendingApply] = useState<Partial<ConsultaFilters> | null>(null);
  // Período que o intervalo De/Até substituiu — `null` quando não foi ele que o zerou.
  // Guarda o VALOR anterior, e não um booleano, porque o caminho de volta é o desfazer da
  // própria operação: quem estava em Março tem de voltar para Março, não para o mês
  // corrente. Ser `null` é o que impede o restauro para quem chegou ao escopo global de
  // propósito (card de KPI, "Buscar") — ali restaurar estreitaria a consulta em silêncio,
  // com o card seguindo aceso, que é a incoerência que o portão existe para evitar.
  const [periodBeforeRange, setPeriodBeforeRange] =
    useState<{ month: number | null; year: number | null } | null>(null);
  // Destino dos controles da toolbar do grid (densidade · colunas · restaurar), que passaram
  // a ocupar a célula livre da 1ª coluna da 2ª linha de filtros — sob a busca genérica.
  // ESTADO, e não `useRef`: o portal precisa re-renderizar quando o nó existir, e um ref
  // mutável não avisa ninguém. O callback ref roda no commit, antes do paint, então o slot
  // já está preenchido no primeiro quadro que o usuário vê.
  const [gridToolbarSlot, setGridToolbarSlot] = useState<HTMLDivElement | null>(null);
  // Destino da barra de seleção do grid (N selecionadas · situação em lote · exportar ·
  // limpar), que passou a viver no CABEÇALHO da página. Mesma mecânica de estado do slot
  // acima, e o motivo do lugar está no comentário do próprio slot, no JSX.
  const [gridSelectionSlot, setGridSelectionSlot] = useState<HTMLDivElement | null>(null);
  // Opções do filtro de empresa (mesmo hook do ContaForm). Lista vazia → o select fica só
  // com "Empresa" (todas), que é justamente o estado sem filtro — não quebra a tela.
  const companyOptions = useCompanyOptions();
  // Opções dos 3 <select> nativos da 2ª linha (centro/grupo/subgrupo): 3 requisições em
  // PARALELO à Next API, disparadas em efeito de montagem — não bloqueiam o primeiro
  // paint nem a consulta do grid (que vai ao Supabase REST, outro host). Lista vazia
  // (carregando ou falhou) → o select fica só com o placeholder, que é "sem filtro".
  const classification = useClassificationFilterOptions();

  const [loadingMore, setLoadingMore] = useState(false);
  const loadingMoreRef = useRef(false);
  // Geração da requisição: só a MAIS RECENTE pode aplicar seu resultado. Sem isso, um
  // append em voo (scroll) que responde DEPOIS de um replace (troca de filtro/ordenação)
  // concatenaria a página da consulta ANTIGA sobre a lista nova — misturando dois
  // conjuntos e duplicando linhas. O `loadingMoreRef` não cobre esse caso: ele serializa
  // appends entre si, não append × replace.
  const requestSeq = useRef(0);

  // Busca os KPIs do filtro APLICADO — `applied`, não `f`: o card tem de refletir a
  // consulta que o grid está mostrando, não o que ainda está sendo digitado na barra.
  // NUNCA rejeita: é chamada em ~8 pontos com `void` e o KPI é acessório — uma falha aqui
  // não pode derrubar o grid nem virar unhandled rejection. Falhou, devolve null, os cards
  // mantêm o último valor bom e o erro vai ao console (não silenciar sem log).
  //
  // A guarda de resposta obsoleta é o `cancelled` do efeito abaixo, e NÃO um ref de
  // geração: um ref lido aqui entraria na cadeia `handleStatusChange → getConsultaColumns`,
  // montada no render, e cairia em `react-hooks/refs` (mesma razão de `pendingApply` ser
  // estado e não ref).
  const fetchStats = useCallback(async (): Promise<FinancialStats | null> => {
    try {
      return await getFinancialStats(applied);
    } catch (e) {
      console.error('Falha ao atualizar os KPIs de /consulta:', e);
      return null;
    }
  }, [applied]);

  // Recarrega os KPIs sob demanda (após curadoria, mudança de situação, exclusão, leitura
  // de e-mails) — sempre com o filtro corrente.
  const refreshStats = useCallback(async () => {
    const st = await fetchStats();
    if (st) setStats(st);
  }, [fetchStats]);

  // Busca uma página de contas e a aplica: `replace` (1ª página / reset de filtro/sort)
  // ou `append` (scroll infinito). `loadingMoreRef` evita disparos concorrentes no append.
  // `result.total` pode ser estimativa (totalIsEstimate) — tratada de forma transparente
  // pelo "hasMore" (rows.length < total), sem mudança visual no rodapé.
  const load = useCallback(
    async (pageNum: number, mode: 'replace' | 'append') => {
      if (mode === 'append') {
        if (loadingMoreRef.current) return;
        loadingMoreRef.current = true;
        setLoadingMore(true);
      } else {
        setLoading(true);
      }
      setError(null);
      const seq = ++requestSeq.current;
      try {
        const result = await getFinancialAccountControl({
          ...applied,
          page: pageNum,
          pageSize: PAGE_SIZE,
          sortCol: sort.col ?? undefined,
          sortDir: sort.dir ?? undefined,
        });
        // Resposta obsoleta (outro load partiu depois deste) → descarta INTEIRA, inclusive
        // total/page: aplicar só parte deixaria o estado incoerente com as linhas em tela.
        if (seq !== requestSeq.current) return;
        // append usa appendUniqueById: com paginação por offset sobre um conjunto que muda
        // (o reader grava a cada 5 min), a página seguinte pode devolver uma linha já
        // exibida. A dedup por `id` é o que garante "a mesma conta nunca aparece 2x".
        setRows((prev) => (mode === 'append' ? appendUniqueById(prev, result.data) : result.data));
        setTotal(result.total);
        setPage(pageNum);
      } catch (e) {
        if (seq !== requestSeq.current) return;
        setError(getErrorMessage(e));
      } finally {
        // O ref é liberado SEMPRE, mesmo em resposta obsoleta — deixá-lo travado
        // impediria qualquer append seguinte (scroll infinito morto, sem erro visível).
        if (mode === 'append') loadingMoreRef.current = false;
        // Já os indicadores só são desligados pela requisição CORRENTE: uma resposta
        // obsoleta apagaria o spinner de uma busca que ainda está em andamento.
        if (seq === requestSeq.current) {
          if (mode === 'append') setLoadingMore(false);
          else setLoading(false);
        }
      }
    },
    [applied, sort],
  );

  // Próxima página (append) — chamado pelo grid (auto ao rolar) e pelo botão "Carregar mais".
  const loadMore = useCallback(() => {
    if (loadingMoreRef.current) return;
    void load(page + 1, 'append');
  }, [load, page]);

  // Reset: quando o filtro aplicado ou a ordenação mudam (e no mount), recomeça da
  // página 1. load() seta `loading` no início — o effect é a ferramenta certa para o
  // fetch-on-change.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load(1, 'replace');
  }, [load]);

  // KPIs a cada mudança de filtro: desde 2026-08-08 os CINCO cards refletem o filtro
  // aplicado (decisão do dono do produto). Antes eram globais e só o VALOR do 1º card era
  // filtrado — o card dizia "R$ 3,7 mi / 742 contas" para um agosto de 211.
  //
  // `cancelled` descarta a resposta de um filtro já trocado: a varredura dos KPIs pagina,
  // então é a chamada mais lenta da página e a resposta ANTIGA pode chegar depois da nova.
  useEffect(() => {
    let cancelled = false;
    void fetchStats().then((st) => {
      if (!cancelled && st) setStats(st);
    });
    return () => {
      cancelled = true;
    };
  }, [fetchStats]);

  // Diretório de usuários (id→e-mail) para o detalhe — busca única no mount. Falha é
  // silenciosa (o detalhe cai no fallback do UUID). void: fire-and-forget idiomático.
  useEffect(() => {
    void getAppUsers().then(setAppUsers).catch(() => undefined);
  }, []);

  // Janela de coalescência do portão de filtros: cada acréscimo ao patch pendente
  // reinicia o timer (o cleanup cancela o anterior), então um usuário compondo vários
  // filtros gera UMA consulta em vez de uma por controle. O cleanup também cobre o
  // desmonte e o cancelamento síncrono (Buscar/Limpar/card/período → pendente = null).
  useEffect(() => {
    if (!pendingApply) return;
    const t = setTimeout(() => {
      setApplied((a) => ({ ...a, ...pendingApply }));
      setPage(1);
      setPendingApply(null);
    }, FILTER_APPLY_DELAY_MS);
    return () => clearTimeout(t);
  }, [pendingApply]);

  // O efeito que buscava "Valor total" + "Total de registros" em DUAS consultas paralelas
  // saiu daqui em 2026-08-08: `getFinancialStats(applied)` já devolve os dois campos, e
  // pelo MESMO recorte dos outros 4 cards. Manter as três consultas significaria três
  // fontes por apply para números que precisam concordar — e era exatamente dessa divisão
  // que nascia o defeito relatado (valor filtrado ao lado de contagem global).

  // Marca/desmarca uma flag de curadoria ("Tem NF" / "Tem Boleto") com update
  // otimista no estado local + persistência via REST; reverte se a gravação falhar.
  const handleToggleFlag = useCallback<ToggleFlag>((row, field, value) => {
    setRows((prev) => prev.map((r) => (r.id === row.id ? { ...r, [field]: value } : r)));
    void setFinancialAccountFlag(row.id, field, value)
      .then(() => {
        // Baixa automática no ATO da edição: com NF + Boleto marcados e vencimento vencido,
        // a conta em aberto vira "pago". Best-effort — falha aqui NÃO reverte a flag já
        // gravada (o batch diário reconcilia o que escapar). Espelha `qualifiesForAutoPago`.
        const next = { ...row, [field]: value };
        if (!qualifiesForAutoPago(next)) return;
        void setFinancialAccountStatus(row.id, STATUS_ID_PAGO)
          .then(() => {
            setRows((prev) => prev.map((r) => (r.id === row.id ? applyStatusId(r, STATUS_ID_PAGO) : r)));
            setSel((s) => (s && s.id === row.id ? applyStatusId(s, STATUS_ID_PAGO) : s));
            void refreshStats();
          })
          .catch((e: unknown) => setError(getErrorMessage(e)));
      })
      .catch((e: unknown) => {
        setRows((prev) => prev.map((r) => (r.id === row.id ? { ...r, [field]: !value } : r)));
        setError(getErrorMessage(e));
      });
  }, [refreshStats]);

  // Altera a situação de uma conta no dropdown inline com update otimista (por status_id).
  // O pai (Consulta) atualiza `rows` após confirmação da API para manter consistência entre
  // a célula editada e o painel de detalhe lateral. Atualiza status_id E o embed status_dim
  // (nome exibido no badge/CSV/detalhe) — a fonte do nome é a dimensão `status`.
  const handleStatusChange = useCallback<StatusChangeCallback>(async (rowId, newStatusId) => {
    await setFinancialAccountStatus(rowId, newStatusId);
    setRows((prev) => prev.map((r) => (r.id === rowId ? applyStatusId(r, newStatusId) : r)));
    void refreshStats();
  }, [refreshStats]);

  const handleBulkStatusChange = useCallback(async (selected: FinancialAccountControl[], newStatusId: number) => {
    const ids = selected.map((r) => r.id);
    setError(null);
    try {
      await setFinancialAccountStatusBulk(ids, newStatusId);
      setRows((prev) => prev.map((r) => (ids.includes(r.id) ? applyStatusId(r, newStatusId) : r)));
      void refreshStats();
    } catch (e) {
      setError(getErrorMessage(e));
    }
  }, [refreshStats]);

  // Salva a edição da conta via Next API (PATCH) e recarrega o grid + KPIs.
  // Devolve ao ContaForm os anexos que NÃO subiram (a fila que deve permanecer) — ver o
  // contrato de `onSubmit` no ContaForm.
  const handleEditSubmit = async (data: FinancialAccountControlCreate, files: File[]): Promise<File[] | void> => {
    if (!editing) return;
    setEditSubmitting(true);
    setEditError(null);
    setEditWarning(null);
    try {
      const updated = await updateConta(editing.id, data);

      // Anexos novos sobem só agora (mesmo caminho da inclusão: nada é enviado antes de a
      // gravação da conta ter dado certo).
      const outcome: UploadOutcome = files.length
        ? await uploadContaAttachments(updated.id, files)
        : { saved: [], failed: [] };
      const { saved, failed } = outcome;

      // A resposta do PATCH foi montada ANTES destes uploads: sem juntar `saved` aqui, os
      // anexos novos só apareceriam num refresh (o grid mescla in-place, sem refetch).
      const merged = { ...updated, attachments: [...(updated.attachments ?? []), ...saved] };

      // Atualiza a linha no lugar (preserva a posição de rolagem do scroll infinito).
      setRows((prev) => prev.map((r) => (r.id === merged.id ? merged : r)));
      setSel((s) => (s && s.id === merged.id ? merged : s));
      void refreshStats();

      if (failed.length) {
        // Modal FICA aberto: a conta foi salva, mas o usuário precisa ver o que faltou.
        // `setEditing(merged)` é essencial: sem ele a lista "Anexos da conta" do modal
        // continuaria sem os que ACABARAM de subir, e o usuário reanexaria tudo.
        setEditing(merged);
        setEditWarning(
          `Conta salva, mas ${failed.length} anexo(s) não foram enviados: ` +
            `${failed.map((f) => f.file.name).join(', ')}. Clique em salvar novamente para tentar só eles.`,
        );
        // Só os que falharam continuam na fila — reenviar os que já subiram os DUPLICARIA.
        return failed.map((f) => f.file);
      }

      setEditing(null);
    } catch (e) {
      // A conta não foi gravada: preserva a fila para o usuário corrigir e tentar de novo.
      setEditError(getErrorMessage(e));
      return files;
    } finally {
      setEditSubmitting(false);
    }
  };

  // Reflete no grid a remoção de um anexo feita pelo modal (sem refetch).
  const handleAttachmentsChanged = (attachments: FinancialAccountControl['attachments']) => {
    if (!editing) return;
    const id = editing.id;
    setEditing((c) => (c ? { ...c, attachments } : c));
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, attachments } : r)));
    setSel((s) => (s && s.id === id ? { ...s, attachments } : s));
  };

  // Hard delete FÍSICO da conta (grupo Administrador). Remove a linha e recarrega os KPIs.
  // Irreversível — só é chamado após a confirmação inline.
  //
  // O ajuste LOCAL de "Valor total"/"Total de registros" saiu daqui em 2026-08-08: com os 5
  // cards vindo de `stats`, decrementar só esses dois deixaria o painel meio atualizado (o
  // total cairia, mas "Pagos"/"Vencidas" continuariam contando a conta removida) até o
  // refresh responder. `refreshStats()` logo abaixo já corrige TODOS de uma vez, a partir
  // do servidor.
  const handleDelete = async (conta: FinancialAccountControl) => {
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteConta(conta.id);
      setRows((prev) => prev.filter((r) => r.id !== conta.id));
      setTotal((t) => Math.max(0, t - 1));
      setSel((s) => (s && s.id === conta.id ? null : s));
      setConfirmDelete(null);
      void refreshStats();
    } catch (e) {
      setDeleteError(getErrorMessage(e));
    } finally {
      setDeleting(false);
    }
  };

  // Abre/fecha o <dialog> nativo de edição (foco/trap/Esc; try/catch p/ jsdom).
  useEffect(() => {
    const el = editDialogRef.current;
    if (!el) return;
    try {
      if (editing) el.showModal();
      else el.close();
    } catch {
      /* showModal indisponível (jsdom) */
    }
  }, [editing]);

  const columns = useMemo(
    () => getConsultaColumns(handleToggleFlag, handleStatusChange),
    [handleToggleFlag, handleStatusChange],
  );

  // Linhas carregadas SEM cancelado (N do rodapé). O grid ainda exibe as canceladas
  // (visíveis), mas elas não entram na contagem — assim "N de M" fica consistente
  // (N ≤ M, ambos sem cancelado). A paginação (hasMore) segue usando `total` (com
  // cancelado) para carregar todas as linhas do grid.
  const loadedNonCancelled = useMemo(
    () => rows.filter((r) => r.status_id !== STATUS_ID_CANCELADO).length,
    [rows],
  );

  // "Atualizar": dispara a leitura IMAP dos últimos 7 dias (job em background no
  // Flask) e acompanha o progresso por poll, recarregando o grid ao vivo e no fim —
  // permite trazer e-mails novos sem abrir a página /emails. Suspende o logout por
  // inatividade durante o processamento (pode levar minutos).
  const handleRefresh = useCallback(async () => {
    if (readingRef.current) return; // já há uma leitura em andamento
    readingRef.current = true;
    setReading(true);
    setError(null);
    setProgress(null);
    suspendIdleLogout();

    try {
      await startEmailRead({ days: REFRESH_DAYS });
    } catch (e) {
      setError(getErrorMessage(e));
      readingRef.current = false;
      setReading(false);
      resumeIdleLogout();
      return;
    }

    try {
      let ticks = 0;
      let errors = 0;
      let polling = true;
      let final: ReadProgress | null = null;
      while (polling) {
        await sleep(PROGRESS_POLL_MS);
        let p: ReadProgress;
        try {
          p = await getEmailReadProgress();
          errors = 0;
        } catch {
          errors += 1;
          if (errors >= PROGRESS_MAX_ERRORS) throw new Error('Perdi contato com o backend durante o processamento.');
          continue;
        }
        setProgress(p);
        ticks += 1;
        if (ticks % GRID_REFRESH_EVERY === 0) {
          void load(1, 'replace'); // grid sobe ao vivo (recomeça da 1ª página)
          void refreshStats();
        }
        if (!p.running) {
          final = p;
          polling = false;
        }
      }
      if (final?.error) setError(final.error);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      readingRef.current = false;
      setReading(false);
      setProgress(null);
      resumeIdleLogout();
      await load(1, 'replace');
      void refreshStats();
    }
  }, [load, refreshStats]);

  // Zera o estado transitório do portão de filtros. Quem aplica de forma SÍNCRONA
  // (Buscar/Limpar/card/período) precisa dos dois: descartar o timer, senão um patch
  // antigo cai por cima depois; e esquecer o período memorizado, porque esses quatro
  // caminhos definem o escopo de propósito — mantê-lo faria um "apagar as datas"
  // posterior restaurar um período obsoleto por cima da escolha do usuário.
  const resetFilterGate = () => {
    setPendingApply(null);
    setPeriodBeforeRange(null);
  };

  // PORTÃO ÚNICO de aplicação dos filtros (busca automática — sem clicar em "Buscar").
  //
  // Escreve em `f` na hora (o controle precisa ecoar o usuário) e acumula o patch no
  // ESTADO `pendingApply` (não num ref — ver o porquê na declaração dele), com UM timer
  // só. Por que não aplicar direto no onChange de cada controle: um apply
  // dispara 3 requisições (grid + "Valor total" + contagem), e compor um filtro de 7
  // controles daria ~21; pior, <select> nativo no Firefox/Windows emite `change` a CADA
  // opção percorrida com as setas do teclado, o que multiplicaria isso por opção. O
  // acúmulo também faz De+Até virarem um apply só, em vez de dois.
  //
  // Lê `f` do closure em vez de um ref: só é chamado de handler de evento, onde o estado
  // do render corrente é o correto (entre dois eventos o React já re-renderizou). A
  // escrita usa a forma FUNCIONAL, então nem uma leitura obsoleta perderia um patch.
  const queueApply = (patch: Partial<ConsultaFilters>) => {
    const merged = { ...f, ...patch };
    const touchesRange = 'dateFrom' in patch || 'dateTo' in patch;
    let derived: Partial<ConsultaFilters> = {};

    if (touchesRange) {
      if (merged.dateFrom !== '' || merged.dateTo !== '') {
        // Intervalo definido vence o período — simétrico ao que os botões de mês já
        // fazem ao limpar dateFrom/dateTo. Sem isso o mês seguiria aceso mentindo, já
        // que a precedência do serviço ignora month/year quando há range.
        derived = { month: null, year: null };
        // Memoriza só quando há MESMO um período para substituir. A guarda é
        // auto-limitante: depois da primeira vez o período já está zerado, então o
        // segundo campo do intervalo não sobrescreve a memória com o valor nulo.
        if (merged.month != null || merged.year != null) {
          setPeriodBeforeRange({ month: merged.month, year: merged.year });
        }
      } else if (periodBeforeRange) {
        // Caminho de VOLTA: apagar as duas datas deixaria o usuário preso em escopo
        // global (toda a base, nenhum mês em destaque) sem nenhuma ação que explicasse.
        // Devolve o período EXATO que o intervalo substituiu — e só existe para quem foi
        // levado até lá PELO intervalo; quem escolheu o escopo global de propósito (card
        // de KPI, "Buscar") tem `periodBeforeRange` nulo e não é estreitado em silêncio.
        derived = periodBeforeRange;
        setPeriodBeforeRange(null);
      }
    }
    // Busca textual é GLOBAL: ao ter texto, procura em toda a base; ao limpar, mantém o
    // período como está (comportamento preservado do debounce anterior).
    if (typeof patch.supplier === 'string' && patch.supplier !== '') {
      derived = { ...derived, month: null, year: null };
    }

    const full = { ...patch, ...derived };
    setF((prev) => ({ ...prev, ...full }));

    // O card ativo é preservado quando o filtro apenas RESTRINGE (empresa, tipo,
    // classificação) — mesmo precedente da navegação por mês — e limpo quando o usuário
    // mexe no campo que o card possui; senão o card "Vencidas" ficaria aceso exibindo
    // contas pagas.
    if ('statusId' in patch || (touchesRange && activeCard === 'avencer7')) setActiveCard(null);

    // Acumula por forma FUNCIONAL: mudanças rápidas somam num patch só, e o efeito
    // abaixo reinicia a janela a cada acréscimo.
    setPendingApply((prev) => ({ ...(prev ?? {}), ...full }));
  };

  // Atalho de uma chave só — a forma como a maioria dos controles chama o portão.
  const qf = <K extends keyof ConsultaFilters>(k: K, v: ConsultaFilters[K]) =>
    queueApply({ [k]: v });

  // Buscar: com a aplicação automática, o que resta a este botão é ALARGAR o escopo —
  // zera o período (mês/ano → "Todas") para procurar em toda a base. Daí o `title`: um
  // botão "Buscar" que troca "Junho" por "todas as datas" sem avisar seria surpreendente.
  const handleSearch = () => {
    resetFilterGate();
    const next = { ...f, month: null, year: null };
    setF(next);
    setApplied(next);
    setActiveCard(null);
    setPage(1);
  };
  // Limpar: volta ao estado padrão (mês/ano corrente por vencimento) e reseta a ordenação.
  const handleClear = () => {
    resetFilterGate();
    const init = initialFilters();
    setF(init);
    setApplied(init);
    setActiveCard(null);
    setSort({ col: null, dir: null });
    setPage(1);
  };

  // Período (campo/mês/ano/"Todas"): aplica imediatamente em f e applied, como o dashboard.
  // NÃO limpa o card ativo: navegar por mês mantém o card destacado (narrows aquele
  // mês), e clicar o card de novo continua sendo o caminho de volta ao mês atual.
  //
  // Deriva de `f`, e NÃO de um patch parcial sobre `applied` (mesmo formato de handleSearch).
  // O motivo é o `resetFilterGate()` da linha acima: ele descarta o patch que ainda espera na
  // janela de 300 ms, mas esse patch JÁ está em `f` (o controle precisa ecoar o usuário na
  // hora). Com `setApplied((a) => ({ ...a, ...patch }))` o filtro escolhido logo antes do
  // clique some da consulta e FICA visível no controle — divergência que não se resolve
  // sozinha, porque `pendingApply` carrega só o patch, nunca `f` inteiro. Partir de `f`
  // incorpora o pendente em vez de perdê-lo.
  const applyPeriod = (patch: Partial<ConsultaFilters>) => {
    resetFilterGate();
    const next = { ...f, ...patch };
    setF(next);
    setApplied(next);
    setPage(1);
  };

  // Ciclo: nenhuma → asc → desc → nenhuma (volta ao padrão created_at.desc).
  const handleSort = (col: string) => {
    setSort((prev) => {
      if (prev.col !== col) return { col, dir: 'asc' };
      if (prev.dir === 'asc') return { col, dir: 'desc' };
      return { col: null, dir: null };
    });
    setPage(1);
  };

  // Cards são GLOBAIS: ativar um mostra TODOS os períodos do status (month/year null);
  // desligar volta ao padrão mês a mês (mês/ano corrente).
  const handleCardFilter = (cardId: string, filterOverride: Partial<ConsultaFilters>) => {
    resetFilterGate();
    setSort({ col: null, dir: null });
    if (activeCard === cardId) {
      setActiveCard(null);
      const init = initialFilters();
      setF(init);
      setApplied(init);
    } else {
      setActiveCard(cardId);
      // O spread de allPeriodFilters() reseta ANTES do override — é o que mantém o card
      // "A vencer em 7 dias" sobre VENCIMENTO mesmo com "Pagamento" escolhido no seletor
      // do intervalo (rangeDateField mora em BASE_FILTERS justamente por isso).
      const next = { ...allPeriodFilters(), ...filterOverride };
      setF(next);
      setApplied(next);
    }
    setPage(1);
  };

  // Rótulo do campo do INTERVALO — é ele, e não o do período, que governa os campos
  // De/Até. Derivá-lo de `f.dateField` faria o leitor de tela anunciar "Vencimento —
  // data inicial" enquanto a consulta usa payment_date, sem nenhum teste ficar vermelho.
  const rangeDateFieldLabel = RANGE_DATE_FIELD_LABEL[f.rangeDateField];
  // Derivado de `f` (o formulário), não de `applied`: o nome tem de descrever o que o clique
  // fará AGORA, e o intervalo digitado já está em `f` mesmo antes da janela de 300 ms fechar.
  const searchName = searchButtonName(f.dateFrom !== '' || f.dateTo !== '');

  const vencidasCount = stats.vencidas ?? 0;
  const cards: MetricCard[] = [
    {
      icon: FileText,
      label: 'Total de registros',
      // Contagem e valor saem da MESMA consulta filtrada. Antes o valor vinha do filtro e
      // a contagem dos KPIs globais: com "Ago/2026" o card dizia "R$ 3.766.725,46 / 742
      // conta(s)" quando agosto tinha 211 — o R$ certo sobre a contagem da base inteira.
      value: stats.totalRecords ?? 0,
      fmt: (v) => v,
      amount: stats.totalValue ?? null,
    },
    {
      icon: CheckCircle2,
      label: 'Pagos',
      value: stats.pago ?? 0,
      fmt: (v) => v,
      amount: stats.pagoValue ?? null,
      success: true,
      cardId: 'pago',
      onCardClick: () => handleCardFilter('pago', { statusId: STATUS_ID_PAGO }),
    },
    {
      icon: Clock,
      label: 'A vencer',
      value: stats.aVencer ?? 0,
      fmt: (v) => v,
      amount: stats.aVencerValue ?? null,
      muted: true,
      cardId: 'avencer',
      onCardClick: () => handleCardFilter('avencer', { statusId: STATUS_ID_A_VENCER }),
    },
    {
      icon: TrendingUp,
      label: 'A vencer em 7 dias',
      value: stats.vencendo ?? 0,
      fmt: (v) => v,
      amount: stats.vencendoValue ?? null,
      muted: true,
      cardId: 'avencer7',
      onCardClick: () => handleCardFilter('avencer7', next7DaysRange()),
    },
    {
      icon: AlertCircle,
      label: 'Vencidas',
      value: vencidasCount,
      fmt: (v) => v,
      amount: stats.vencidasValue ?? null,
      danger: vencidasCount > 0,
      cardId: 'vencidas',
      onCardClick: () => handleCardFilter('vencidas', { statusId: STATUS_ID_VENCIDO }),
    },
  ];

  return (
    <div className="flex flex-col h-full">
      {/* Barra superior em gradiente (2px) — acento de marca */}
      <div className="h-0.5 bg-linear-to-r from-brand to-brand-dark" />
      <div className="px-6 py-1 border-b border-slate-200 bg-white flex items-center justify-between gap-3">
        {/* `truncate` nos DOIS textos, e não só `min-w-0` no bloco: com a barra de seleção
            ocupando o meio desta linha, um notebook 1366×768 (≈1.110px úteis com a sidebar)
            fica no limite — título ~190px + barra ~580px + botões ~330px + gaps/padding.
            Sob essa pressão o texto QUEBRARIA em duas linhas e o cabeçalho passaria de 38px
            para ~58px, empurrando o grid ao marcar a primeira conta: o mesmo salto que trazer
            a barra para cá existe para eliminar. Com `truncate` o bloco encolhe com
            reticências e a altura da linha não muda. */}
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold text-slate-800">Consulta de movimentações</h1>
          <p className="truncate text-xs text-slate-500 mt-0.5">Contas a pagar</p>
        </div>
        {/* Destino, por PORTAL a partir do DataGrid (`toolbarSelectionTarget`), da barra de
            seleção do grid — N selecionadas · situação em lote · exportar · limpar.

            🔴 Ela morava numa faixa logo acima do cabeçalho do grid, e essa faixa tinha de
            reservar 48px MESMO VAZIA: sem a reserva, marcar a primeira linha empurrava o
            grid para baixo sob o ponteiro e o clique seguinte, numa baixa em lote, caía na
            linha errada. Ou seja, pagava-se 48px de altura de grid em toda sessão para
            proteger um clique.

            Aqui os dois lados são atendidos: esta linha já mede 38px por causa do bloco do
            título, a barra cabe nela inteira (ver o cálculo do padding em `GridToolbar`) e
            nada salta ao aparecer — enquanto os 48px voltam a ser linhas de dado. De brinde,
            o cabeçalho está FORA da área rolável (`overflow-y-auto` começa no <div> abaixo),
            então as ações em lote continuam ao alcance com o grid rolado.

            Nasce VAZIO: sem `aria-hidden` (o conteúdo que chega é interativo) e sem largura
            própria, então com `justify-between` o título e os botões seguem nas pontas. */}
        <div ref={setGridSelectionSlot} className="min-w-0" />
        <div className="flex gap-2 shrink-0">
          <button onClick={() => exportCsv(rows)} className="btn" disabled={!rows.length}>
            <Download size={14} /> Exportar carregados ({rows.length})
          </button>
          {/* "Atualizar" dispara a leitura IMAP (Flask local) — oculto em produção. */}
          {EMAIL_READER_ENABLED && (
            <button
              onClick={handleRefresh}
              className="btn"
              disabled={reading || loading}
              title="Buscar e-mails dos últimos 7 dias e atualizar a consulta"
            >
              <RefreshCw size={14} className={reading || loading ? 'animate-spin' : ''} />
              Atualizar
            </button>
          )}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-6 py-3">
        {error && (
          <Alert variant="error" className="mb-4">
            <strong>Erro:</strong> {error}
          </Alert>
        )}

        {reading && (
          <Alert variant="info" className="mb-4">
            Buscando e-mails dos últimos 7 dias…
            {progress && progress.total > 0 ? ` (${progress.done}/${progress.total})` : ''}
          </Alert>
        )}

        <div className="flex gap-2 mb-2 flex-wrap">
          {cards.map(({ icon: Icon, label, value, fmt, amount, danger, success, muted, cardId, onCardClick }) => {
            const isActive = !!cardId && activeCard === cardId;
            const borderLeft = danger ? 'border-l-status-error-solid' : success ? 'border-l-status-success-fg' : 'border-l-brand';
            let cardBg = 'bg-white';
            if (isActive) {
              cardBg = danger
                ? 'bg-status-error-bg ring-1 ring-status-error-border/40'
                : success
                  ? 'bg-status-success-bg ring-1 ring-status-success-border/40'
                  : 'bg-brand/5 ring-1 ring-brand/30';
            }
            const interactive = onCardClick ? 'cursor-pointer hover:shadow-md hover:scale-[1.01]' : '';
            const iconCls = danger ? 'bg-status-error-solid/10 text-status-error-fg' : success ? 'bg-status-success-bg text-status-success-fg' : 'bg-brand/10 text-brand';
            const valueCls = danger ? 'text-status-error-fg' : success ? 'text-status-success-fg' : muted ? 'text-slate-500' : 'text-slate-800';
            return (
              <div
                key={label}
                role={onCardClick ? 'button' : undefined}
                tabIndex={onCardClick ? 0 : undefined}
                onClick={onCardClick}
                onKeyDown={onCardClick ? (e: React.KeyboardEvent) => { if (e.key === 'Enter' || e.key === ' ') onCardClick(); } : undefined}
                className={`flex-1 min-w-[140px] flex items-center gap-2 rounded-lg p-2 border-l-2 shadow-xs hover:shadow-sm transition-shadow animate-fade-in-up ${borderLeft} ${cardBg} ${interactive}`}
              >
                <div className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full ${iconCls}`}>
                  <Icon size={14} />
                </div>
                <div className="min-w-0">
                  {amount != null && (
                    <div className={`text-xl font-semibold leading-tight truncate ${valueCls}`}>
                      {fmtMoney(amount)}
                    </div>
                  )}
                  <div className={`text-lg leading-tight ${valueCls}`}>
                    {fmt(value)}
                    <span className="text-xs font-normal text-slate-500 ml-1">conta(s)</span>
                  </div>
                  <div className="text-xs text-slate-500 truncate">{label}</div>
                </div>
              </div>
            );
          })}
        </div>

        {/* Período: tipo de data (vencimento/emissão) + mês + ano + "Todas" — mesmo
            princípio do Dashboard. O seletor "Tipo de data" fica AQUI, junto do período
            que ele controla. Aplica imediatamente; o grid e o card "Valor total" seguem
            o período (cards de KPI ficam globais). */}
        <div className="flex items-center justify-start gap-3 flex-wrap mb-2">
          {/* Tipo de data: coluna usada pelos BOTÕES DE MÊS/ANO — e só por eles. O
              intervalo De/Até tem seletor PRÓPRIO e independente, na grade abaixo (ele
              oferece "Pagamento", que o período não oferece). Aplica imediatamente.

              O wrapper existe pela mesma razão do seletor do intervalo: pendurar a ressalva
              como texto de VERDADE na árvore acessível. Aqui ela responde "por que trocar a
              coluna não mudou nada agora?" — com o intervalo preenchido o período fica
              suspenso. Ver PERIOD_DATE_FIELD_HINT, inclusive por que o controle NÃO é
              desabilitado nesse estado. */}
          <div>
            <select
              id="consulta-date-field"
              name="consulta-date-field"
              aria-label="Tipo de data do período (vencimento ou emissão)"
              aria-describedby="consulta-date-field-hint"
              title={PERIOD_DATE_FIELD_HINT}
              className="input h-7 w-32 py-0 text-xs"
              value={f.dateField}
              onChange={(e) => applyPeriod({ dateField: e.target.value as ConsultaFilters['dateField'] })}
            >
              <option value="due_date">Vencimento</option>
              <option value="issue_date">Emissão</option>
            </select>
            <span id="consulta-date-field-hint" className="sr-only">
              {PERIOD_DATE_FIELD_HINT}
            </span>
          </div>

          <div className="flex gap-0.5 flex-wrap" role="group" aria-label="Filtrar por mês">
            {MONTHS.map((m, i) => (
              <button
                key={m}
                onClick={() => applyPeriod({ month: i, year: f.year ?? nowRef.year, dateFrom: '', dateTo: '' })}
                aria-label={`Mês ${MONTHS_FULL[i]}`}
                aria-pressed={f.month === i}
                className={`text-xs px-2 py-0.5 rounded-sm transition-colors ${f.month === i ? 'bg-brand-light text-brand-dark font-semibold' : 'text-slate-500 hover:bg-slate-100'}`}
              >
                {m}
              </button>
            ))}
          </div>

          <div className="flex gap-1" role="group" aria-label="Filtrar por ano">
            {[(f.year ?? nowRef.year) - 1, f.year ?? nowRef.year, (f.year ?? nowRef.year) + 1].map((y) => (
              <button
                key={y}
                onClick={() => applyPeriod({ year: y, month: f.month ?? nowRef.month, dateFrom: '', dateTo: '' })}
                aria-pressed={f.year === y}
                className={`text-xs font-medium px-2.5 py-1 rounded-md border transition-colors ${f.year === y ? 'bg-brand-dark border-brand-dark text-white' : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50'}`}
              >
                {y}
              </button>
            ))}
          </div>

          <button
            onClick={() => applyPeriod({ month: null, year: null, dateFrom: '', dateTo: '' })}
            aria-label="Todas as datas"
            aria-pressed={f.month === null}
            className={`text-xs font-medium px-2.5 py-1 rounded-md border transition-colors ${f.month === null ? 'bg-brand-dark border-brand-dark text-white' : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50'}`}
          >
            Todas
          </button>
        </div>

        {/* GRADE DOS FILTROS — as duas linhas num ÚNICO grid, para que as colunas se
            alinhem entre si (busca↔plano, empresa↔sub grupo, tipo doc↔grupo, tipo
            pagamento↔centro de custo, buscar↔limpar). O template é declarado UMA vez:
            "mesma largura" deixa de ser dois `w-*` mantidos à mão em pontos distantes do
            arquivo e passa a ser estrutural — desalinhar por edição parcial fica
            impossível. Por isso os controles NÃO levam `w-*`: o `w-full` que o
            `@utility input` já traz preenche a célula, e os `.btn` (inline-flex) são
            esticados pelo grid, o que iguala Buscar e Limpar por construção.

            `overflow-x-auto` + `w-max`: grid não quebra, transborda. Confinar o overflow
            AQUI faz as duas linhas rolarem JUNTAS (o alinhamento sobrevive em tela
            estreita, ao contrário do flex-wrap, que o destruía) e impede que o scroll
            lateral vaze para a página — o container externo é `overflow-y-auto`, cujo
            overflow-x computa para `auto`, e arrastar o DataGrid junto quebraria as
            colunas fixadas (position: sticky).

            Tracks em comprimento explícito, nunca `1fr` cru: `fr` tem mínimo
            `min-content`, e o `min-content` de um <select> é a opção mais longa — as
            descrições de centro de custo estourariam a coluna.

            O mínimo da coluna 1 é 25rem (era 22,5rem) porque ela deixou de servir só à
            busca: abaixo dela ficam os controles da toolbar do grid, cujos botões
            (Confortável · Compacto · Colunas · Restaurar) somam ~24,5rem. Com 22,5rem eles
            invadiriam a coluna 2 na largura mínima da grade. */}
        <div className="overflow-x-auto mb-2">
          <div className="grid w-full min-w-max grid-cols-[minmax(25rem,1fr)_16.5rem_11rem_10rem_10rem_8.5rem_8.5rem_8.5rem] items-center gap-2">
          <div className="relative">
            <input
              id="consulta-supplier"
              name="consulta-supplier"
              aria-label="Buscar por fornecedor, CNPJ, número do documento, valor, assunto, remetente, e-mail do fornecedor, centro de custo, plano de contas, grupo ou subgrupo"
              className="input pr-8"
              placeholder="Fornecedor, Nº doc, valor, assunto, centro de custo, plano de contas…"
              value={f.supplier}
              onChange={(e) => qf('supplier', e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleSearch();
              }}
            />
            {f.supplier && (
              <button
                type="button"
                aria-label="Limpar busca"
                onClick={() => qf('supplier', '')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-600"
              >
                <X size={14} />
              </button>
            )}
          </div>
          {/* Empresa pagadora — logo após a busca (espelha a ordem do grid: Fornecedor →
              Empresa). Vazio = TODAS. Filtra o grid e os cards "Valor total"/"Total de
              registros"; os KPIs gerais são globais por design. */}
          <select
            id="consulta-company"
            name="consulta-company"
            aria-label="Filtrar por empresa"
            className="input"
            value={f.skCompany == null ? '' : String(f.skCompany)}
            onChange={(e) => qf('skCompany', e.target.value ? Number(e.target.value) : undefined)}
          >
            <option value="">Empresa</option>
            {companyOptions.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
          <select id="consulta-doc-type" name="consulta-doc-type" aria-label="Filtrar por tipo de documento" className="input" value={f.docType} onChange={(e) => qf('docType', e.target.value)}>
            <option value="">Tipo Documento</option>
            {DOCUMENT_TYPES.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
          <select id="consulta-payment-method" name="consulta-payment-method" aria-label="Filtrar por tipo de pagamento" className="input" value={f.paymentMethod} onChange={(e) => qf('paymentMethod', e.target.value)}>
            <option value="">Tipo Pagamento</option>
            {PAYMENT_METHODS.map((m) => (
              <option key={m}>{m}</option>
            ))}
          </select>
          <select
            id="consulta-status"
            name="consulta-status"
            aria-label="Filtrar por situação"
            className="input"
            value={f.statusId == null ? '' : String(f.statusId)}
            onChange={(e) => qf('statusId', e.target.value ? Number(e.target.value) : undefined)}
          >
            <option value="">Situação</option>
            {STATUS_FILTER_OPTIONS.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>
          {/* Intervalo De/Até — filtra pela coluna do seletor LOGO ABAIXO (independente
              do "Tipo de data" da linha dos meses). Aplica sozinho; sem Enter, que aqui
              significaria "alargar o período", não "aplicar". */}
          <input
            id="consulta-date-from"
            name="consulta-date-from"
            aria-label={`${rangeDateFieldLabel} — data inicial`}
            type="date"
            className="input"
            value={f.dateFrom}
            max={f.dateTo || undefined}
            onChange={(e) => qf('dateFrom', e.target.value)}
            title={`${rangeDateFieldLabel} de`}
          />
          <input
            id="consulta-date-to"
            name="consulta-date-to"
            aria-label={`${rangeDateFieldLabel} — data final`}
            type="date"
            className="input"
            value={f.dateTo}
            min={f.dateFrom || undefined}
            onChange={(e) => qf('dateTo', e.target.value)}
            title={`${rangeDateFieldLabel} até`}
          />
          {/* Seletor da coluna de data do INTERVALO — fecha a 1ª linha (coluna 8), ao lado
              dos campos De/Até que ele governa. `col-start-8` é explícito e não decorativo:
              é o que documenta a posição e o que o teste de alinhamento observa.
              O nome acessível NÃO pode conter "Tipo de data" — é o nome do seletor do
              período, e o teste que o localiza passaria a casar dois elementos.

              O wrapper existe para abrigar a ressalva de dado como texto de VERDADE na
              árvore acessível: `title` sozinho não aparece no toque, não é focável e,
              com `aria-label` presente, não é anunciado de forma confiável — e é essa
              ressalva que explica por que "Pagamento" + Situação "a vencer" devolve 0
              linhas. O `sr-only` é posicionado, mas fica DENTRO do wrapper em vez de
              solto no grid, para não depender de "item absoluto não ocupa track". */}
          <div className="col-start-8 min-w-0">
            <select
              id="consulta-range-date-field"
              name="consulta-range-date-field"
              aria-label="Data do intervalo (De/Até)"
              aria-describedby="consulta-range-date-field-hint"
              title={RANGE_DATE_FIELD_HINT}
              className="input"
              value={f.rangeDateField}
              onChange={(e) => qf('rangeDateField', e.target.value as RangeDateField)}
            >
              {Object.entries(RANGE_DATE_FIELD_LABEL).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <span id="consulta-range-date-field-hint" className="sr-only">
              {RANGE_DATE_FIELD_HINT}
            </span>
          </div>

          {/* ── 2ª linha da MESMA grade ──────────────────────────────────────────────
              Abre na COLUNA 1, sob a busca genérica, com os controles da toolbar do grid
              (densidade · colunas · restaurar) — antes soltos acima do grid. Eles chegam
              aqui por PORTAL a partir do DataGrid (`toolbarControlsTarget`), porque o
              estado de layout do grid vive no `useGridPreferences` dele; elevá-lo para cá
              tocaria todas as telas com grid para atender só a esta.

              O <div> é o destino do portal e nasce VAZIO — por isso não leva `aria-hidden`
              (o conteúdo real é interativo) nem texto. Ocupa a coluna 1 por auto-placement,
              que é o que empurra o cursor para a coluna 2, onde o plano de contas ancora. */}
          <div ref={setGridToolbarSlot} className="min-w-0" />

          {/* ── Classificação contábil (colunas 2 a 5) ───────────────────────────────
              Os 4 são INDEPENDENTES (combinados por AND, sem cascata) e, como os da 1ª
              linha, aplicam sozinhos. Plano de contas é busca digitável: são ~530
              descrições, volume demais para um <select> nativo; variant="filter" NÃO
              carrega a lista na abertura da página (só no 1º clique que abre o menu).
              `min-w-0` porque o react-select, sem ele, empurra o track da coluna.

              `col-start-2` mantém cada filtro contábil sob o controle correspondente da 1ª
              linha — plano↔Empresa, sub grupo↔Tipo Documento, grupo↔Tipo Pagamento, centro
              de custo↔Situação. Hoje o slot da toolbar já ocupa a coluna 1 e deixaria o
              cursor aqui de qualquer forma; a âncora fica EXPLÍCITA porque remover o slot
              (ou movê-lo) puxaria os quatro filtros uma coluna à esquerda em silêncio.
              Só este item é posicionado à mão; os três seguintes caem nas colunas 3, 4 e 5
              pelo auto-placement, que nunca anda para trás. */}
          <div className="col-start-2 min-w-0">
            <ChartAccountSelect
              id="consulta-chart-account"
              variant="filter"
              label="Filtrar por plano de contas"
              value={f.chartAccountDescription || null}
              onChange={(d) => qf('chartAccountDescription', d ?? '')}
            />
          </div>
          <select
            id="consulta-chart-subgroup"
            name="consulta-chart-subgroup"
            aria-label="Filtrar por sub grupo de plano de contas"
            className="input"
            value={f.chartAccountSubgroupId == null ? '' : String(f.chartAccountSubgroupId)}
            onChange={(e) => qf('chartAccountSubgroupId', e.target.value ? Number(e.target.value) : undefined)}
          >
            <option value="">Sub grupo</option>
            {classification.subgroups.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
          <select
            id="consulta-chart-group"
            name="consulta-chart-group"
            aria-label="Filtrar por grupo de plano de contas"
            className="input"
            value={f.chartAccountGroupId == null ? '' : String(f.chartAccountGroupId)}
            onChange={(e) => qf('chartAccountGroupId', e.target.value ? Number(e.target.value) : undefined)}
          >
            <option value="">Grupo</option>
            {classification.groups.map((g) => (
              <option key={g.value} value={g.value}>
                {g.label}
              </option>
            ))}
          </select>
          <select
            id="consulta-cost-center"
            name="consulta-cost-center"
            aria-label="Filtrar por centro de custo"
            className="input"
            value={f.costCenterId == null ? '' : String(f.costCenterId)}
            onChange={(e) => qf('costCenterId', e.target.value ? Number(e.target.value) : undefined)}
          >
            <option value="">Centro de custo</option>
            {classification.costCenters.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
          {/* Os DOIS botões fecham a 2ª linha, lado a lado (colunas 7 e 8). `col-start-7`
              é obrigatório: o cursor do auto-placement pararia na 6 (livre desde que o
              seletor de data subiu para a 1ª linha) e os botões ficariam deslocados uma
              coluna à esquerda. O `col-start-8` do "Limpar" é redundante em relação ao
              vizinho, mas fica explícito para que mexer num não desloque o outro em
              silêncio — os dois estão travados no teste de alinhamento.

              Com a aplicação automática, "Buscar" não "aplica" mais — ele ALARGA o escopo.
              O nome acessível diz isso; o rótulo visível segue "Buscar" (copy de produto), e
              o nome o CONTÉM, como exige a WCAG 2.5.3 (Label in Name). Sem o aria-label, a
              única pista da mudança de escopo era o `title`, invisível para teclado e leitor
              de tela. O nome varia com o estado porque o efeito varia — ver
              `searchButtonName`. */}
          <button
            onClick={handleSearch}
            className="btn btn-primary col-start-7"
            aria-label={searchName}
            title={searchName}
          >
            <Search size={14} /> Buscar
          </button>
          <button onClick={handleClear} className="btn justify-center col-start-8">
            Limpar
          </button>
          </div>
        </div>

        <div className="card mb-2">
          <DataGrid
            columns={columns}
            rows={rows}
            rowKey={(r) => String(r.id)}
            selectedId={sel ? String(sel.id) : null}
            onRowClick={(r) => setSel(sel?.id === r.id ? null : r)}
            // Conta cancelada: linha inteira em vermelho mais saturado (status-error-solid/15)
            // — tom distinto do vermelho pálido do badge "vencido" (status-error-bg).
            rowClassName={(r) => (r.status_id === STATUS_ID_CANCELADO ? 'bg-status-error-solid/15' : undefined)}
            sortCol={sort.col}
            sortDir={sort.dir}
            onSort={handleSort}
            gridId="consulta"
            enableColumnManagement
            enableSelection
            enableRowVirtualization
            hasMore={rows.length < total}
            loadingMore={loadingMore}
            onLoadMore={loadMore}
            defaultPinning={CONSULTA_DEFAULT_PINNING}
            defaultDensity="compact"
            // Densidade · colunas · restaurar sobem para a 1ª coluna da 2ª linha de filtros;
            // a barra de seleção vai para o cabeçalho da página. Com os dois fora, não sobra
            // nada acima do cabeçalho do grid — nem a faixa de 48px que só existia para
            // reservar a altura da barra de seleção (ver o slot no cabeçalho).
            toolbarControlsTarget={gridToolbarSlot}
            toolbarSelectionTarget={gridSelectionSlot}
            onExportSelected={exportCsv}
            bulkStatusOptions={STATUS_OPTIONS}
            onBulkStatusChange={handleBulkStatusChange}
            maxBodyHeight="78vh"
            loading={loading}
            emptyMessage={loading ? 'Buscando registros…' : emptyGridMessage(applied)}
            // Rodapé SEMPRE-visível abaixo das células: a informação adicional do registro,
            // destacada em fonte (Jakarta itálica) e cor (brand) distintas das células.
            renderRowFooter={(r) =>
              r.additional_info ? (
                <div className="flex items-start gap-1.5 px-3 py-1.5 font-jakarta text-xs italic text-slate-600 whitespace-pre-wrap">
                  <Info size={13} className="mt-0.5 shrink-0 text-slate-500" aria-hidden="true" />
                  <span>
                    <span className="font-semibold not-italic">Informação adicional:</span> {r.additional_info}
                  </span>
                </div>
              ) : null
            }
            renderDetail={(r) => (
                          <div className="relative bg-slate-50/60 border-l-2 border-brand p-4">
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setSel(null);
                              }}
                              className="absolute right-3 top-3 flex h-7 w-7 items-center justify-center rounded-full text-slate-500 hover:bg-slate-200/60 hover:text-slate-600 transition-colors"
                              title="Fechar"
                            >
                              <X size={15} />
                            </button>
                            <p className="text-xs font-semibold text-slate-500 mb-3 uppercase tracking-wide pr-8">
                              Detalhes — {(r.supplier?.trade_name ?? r.supplier?.legal_name) || 'registro'} · {fmtDate(r.due_date)}
                            </p>
                            <div className="mb-3 flex flex-wrap items-center gap-2">
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setEditError(null);
                                  setEditWarning(null);
                                  setEditing(r);
                                }}
                                className="btn btn-primary"
                                title="Editar esta conta"
                              >
                                <Pencil size={14} /> Editar conta
                              </button>

                              {/* Hard delete — só o grupo Administrador. Confirmação inline
                                  (irreversível). Os botões contêm o próprio clique (não alternar
                                  a linha do <tr>). */}
                              {isAdminGroup &&
                                (confirmDelete === r.id ? (
                                  <span className="flex items-center gap-2">
                                    <span className="text-xs font-medium text-status-error-fg">
                                      Excluir permanentemente?
                                    </span>
                                    <button
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        void handleDelete(r);
                                      }}
                                      disabled={deleting}
                                      className="inline-flex items-center gap-1 rounded-md bg-status-error-fg px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-60"
                                    >
                                      <Trash2 size={14} /> {deleting ? 'Excluindo…' : 'Excluir'}
                                    </button>
                                    <button
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        setConfirmDelete(null);
                                        setDeleteError(null);
                                      }}
                                      disabled={deleting}
                                      className="btn"
                                    >
                                      Cancelar
                                    </button>
                                  </span>
                                ) : (
                                  <button
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setDeleteError(null);
                                      setConfirmDelete(r.id);
                                    }}
                                    className="inline-flex items-center gap-1 rounded-md border border-status-error-border px-3 py-1.5 text-sm font-medium text-status-error-fg hover:bg-status-error-bg"
                                    title="Excluir permanentemente esta conta (grupo Administrador)"
                                  >
                                    <Trash2 size={14} /> Excluir conta
                                  </button>
                                ))}
                            </div>
                            {deleteError && confirmDelete === r.id && (
                              <p className="mb-3 text-xs text-status-error-fg">{deleteError}</p>
                            )}
                            {/* Anexos (N) — e-mail + enviados pelo usuário, no mesmo padrão.
                                Substituiu o botão único "Ver anexo" (era 1 arquivo só, o do
                                e-mail). Sem lixeira aqui: a remoção é feita pelo modal de
                                edição. Quem contém o clique para não alternar a linha do <tr>
                                são os próprios botões (AttachmentList/AttachmentViewer), como
                                fazem os botões acima — não um <div> wrapper com onClick, que
                                seria um elemento não-interativo com handler (S1082). */}
                            <div className="mb-3">
                              <ContaAttachments
                                accountId={r.id}
                                items={r.attachments}
                                legacySourceFile={r.source_file}
                                canRemove={false}
                              />
                            </div>
                            <dl className="grid grid-cols-2 rounded-lg overflow-hidden border border-slate-100">
                              {(
                                [
                                  ['ID', String(r.id)],
                                  ['Fornecedor', fmtSupplier(r)],
                                  // Empresa PAGADORA (company.trade_name via FK sk_company) —
                                  // logo APÓS o Fornecedor, espelhando a ordem do grid. São coisas
                                  // distintas: a conta pode ser da LEBIANCO e o fornecedor, OTIMOTEX.
                                  ['Empresa', r.company?.trade_name ?? '—'],
                                  ['Assunto', r.subject],
                                  ['Remetente', r.sender_email],
                                  ['CNPJ', fmtCnpj(r.supplier?.cnpj ?? null)],
                                  ['N° Documento', r.invoice_number],
                                  ['Competência', r.competence_date],
                                  ['Emissão', fmtDate(r.issue_date)],
                                  ['Vencimento', fmtDate(r.due_date)],
                                  ['Situação', r.status_dim?.status_name ?? '—'],
                                  ['Valor do documento', fmtMoney(r.amount)],
                                  ['Valor cobrado', fmtMoney(r.amount_charged)],
                                  ['Desconto / abatimentos', fmtMoney(r.discount)],
                                  ['Outras deduções', fmtMoney(r.other_deductions)],
                                  ['Mora / multa', fmtMoney(r.fine_interest)],
                                  ['Outros acréscimos', fmtMoney(r.other_additions)],
                                  ['Nosso número', r.nosso_numero || '—'],
                                  ['Centro de custo', fmtCostCenter(r)],
                                  ['Plano de contas', fmtChartAccount(r)],
                                  ['Forma de pag.', r.payment_method],
                                  ['Código de barras', r.barcode || '—'],
                                  ['Origem', r.source_file],
                                  ['Observações', r.processing_notes || '—'],
                                  ['Criado por', userEmail(r.created_by)],
                                  ...(isSentinelAuthor(r.updated_by)
                                    ? ([] as [string, string | null][])
                                    : ([['Última edição por', userEmail(r.updated_by)]] as [string, string | null][])),
                                  ...(isSentinelAuthor(r.status_changed_by)
                                    ? ([] as [string, string | null][])
                                    : ([[
                                        'Situação alterada por',
                                        `${userEmail(r.status_changed_by)} · ${fmtDateTime(r.status_changed_at)}`,
                                      ]] as [string, string | null][])),
                                ] as [string, string | null][]
                              ).map(([k, v], i) => (
                                <div
                                  key={k}
                                  className={`flex gap-3 px-3 py-1.5 ${
                                    Math.floor(i / 2) % 2 === 0 ? 'bg-slate-50/30' : 'bg-white'
                                  }`}
                                >
                                  <dt className="w-36 shrink-0 text-slate-500 text-xs">{k}</dt>
                                  <dd className="text-slate-700 text-xs break-all">{v ?? '—'}</dd>
                                </div>
                              ))}
                            </dl>
                            {r.description && (
                              <div className="mt-3 p-3 bg-white rounded-lg border border-slate-100">
                                <span className="badge bg-brand/10 text-brand mb-2">Descrição</span>
                                <p className="text-xs text-slate-600">{r.description}</p>
                              </div>
                            )}
                            {r.additional_info && (
                              <div className="mt-3 p-3 bg-white rounded-lg border border-slate-100">
                                <span className="badge bg-brand/10 text-brand mb-2">Informações adicionais</span>
                                <p className="text-xs text-slate-600 whitespace-pre-wrap">{r.additional_info}</p>
                              </div>
                            )}
                            {r.email_body_excerpt && (
                              <div className="mt-3 p-3 bg-white rounded-lg border border-slate-100">
                                <span className="badge bg-brand/10 text-brand mb-2">Mensagem do e-mail</span>
                                <ExpandableText text={r.email_body_excerpt} />
                              </div>
                            )}
                          </div>
            )}
          />
        </div>

        {/* pl-1/pr-20 (não px-1): o pr-20 abre espaço para o botão flutuante do assistente de IA
            (fixed bottom-5 right-5, 48px) não cobrir o "Carregar mais", que é o único controle
            no canto inferior direito do app. Classes de padding separadas de propósito — `px-1`
            + `pr-20` na mesma lista dependeria da ordem do CSS gerado para decidir o vencedor. */}
        <div className="flex items-center justify-between py-2 pl-1 pr-20 mb-4">
          <span className="text-xs text-slate-500">
            {/* Mesma fonte do card "Total de registros" — a página exibia dois números sob
                o mesmo nome (o rodapé filtrado, o card global) e eles discordavam. */}
            {loadedNonCancelled} de {stats.totalRecords ?? loadedNonCancelled} registros
            {stats.totalValue != null && ` · Valor total: ${fmtMoney(stats.totalValue)}`}
          </span>
          {rows.length < total && (
            <button
              onClick={loadMore}
              disabled={loadingMore || loading}
              className="btn disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {loadingMore ? 'Carregando…' : 'Carregar mais'}
            </button>
          )}
        </div>
      </div>


      {editing && (
        <dialog
          ref={editDialogRef}
          aria-label="Editar conta"
          onCancel={() => setEditing(null)}
          className="fixed inset-0 m-auto h-fit max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-xl border-0 bg-white p-0 shadow-lg backdrop:bg-black/50"
        >
          <div className="p-4">
            <h2 className="mb-3 text-base font-semibold text-gray-900">Editar conta</h2>
            {editWarning && (
              <Alert variant="warning" className="mb-3">
                {editWarning}
              </Alert>
            )}
            <ContaForm
              mode="edit"
              defaultValues={editing}
              onSubmit={handleEditSubmit}
              onCancel={() => setEditing(null)}
              submitError={editError}
              submitting={editSubmitting}
              onAttachmentsChanged={handleAttachmentsChanged}
            />
          </div>
        </dialog>
      )}
    </div>
  );
}
