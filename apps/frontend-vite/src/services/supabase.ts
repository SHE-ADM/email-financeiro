// src/services/supabase.ts
// Acesso direto à REST API do Supabase — sem dependência do cliente oficial
// para leitura de dados (o cliente oficial em lib/supabaseClient.ts cuida
// apenas da sessão de autenticação).
//
// As policies de RLS exigem o papel `authenticated` (migration 015) — por
// isso o header Authorization carrega o token da sessão do usuário logado,
// não a anon key. O `apikey` continua sendo a anon key: ela só identifica o
// projeto perante o Supabase, quem define o papel para o RLS é o JWT do
// Authorization.

import type { EmailControl, FinancialAccountControl, ProcessingError } from '@sheild/shared';
import {
  STATUS_ID_CANCELADO,
  STATUS_ID_PAGO,
  STATUS_ID_VENCIDO,
  STATUS_ID_A_VENCER,
  STATUS_NAME_BY_ID,
  TYPE_GROUP_ID_DESPESAS,
  TYPE_GROUP_ID_CUSTO,
  TYPE_GROUP_ID_DESPESA_FIXA,
  TYPE_GROUP_ID_DESPESA_VARIAVEL,
  TYPE_GROUP_ID_CUSTO_MERCADORIAS,
  TYPE_GROUP_ID_CUSTO_IMPORTACAO,
} from '@sheild/shared';
import { supabase } from '../lib/supabaseClient';
import { stableOrder } from '../lib/stableOrder';
import { todayISO, isoDaysFromToday } from '../lib/format';

// Situação é filtrada/ordenada por status_id (fonte única). Ordenar a coluna
// "Situação" continua ALFABÉTICO pelo NOME (decisão de negócio — id ≠ ordem), via o
// embed da dimensão: order=status_dim(status_name). Mapeia a chave de coluna do grid.
const STATUS_SORT_KEY = 'status';
const STATUS_DIM_ORDER = 'status_dim(status_name)';

const BASE_URL = import.meta.env.VITE_SUPABASE_URL;
const ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY;

type QueryParams = Record<string, string | number | undefined>;

async function authHeaders(extra: Record<string, string> = {}): Promise<Record<string, string>> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token ?? ANON_KEY;
  return {
    apikey: ANON_KEY,
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json',
    ...extra,
  };
}

async function query<T>(table: string, params: QueryParams = {}): Promise<T> {
  const url = new URL(`${BASE_URL}/rest/v1/${table}`);
  Object.entries(params).forEach(([k, v]) => v !== undefined && url.searchParams.set(k, String(v)));
  const res = await fetch(url.toString(), { headers: await authHeaders() });
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${await res.text()}`);
  return res.json() as Promise<T>;
}

// ── company ──────────────────────────────────────────────────────────────────

// E-mail da empresa pagadora (cadastro `company`, sk_company=1) — exibido como
// subtítulo em /emails: é a caixa de onde os e-mails são lidos. RLS: policy
// `authenticated read` (qual `true`). Retorna null se não encontrado/sem acesso.
// sk_company é a chave de relacionamento única (migration 083; company_id ficou
// só como campo de origem do sistema maior).
export async function getCompanyEmail(skCompany = 1): Promise<string | null> {
  const rows = await query<{ email: string | null }[]>('company', {
    select: 'email',
    sk_company: `eq.${skCompany}`,
    limit: 1,
  });
  return rows[0]?.email ?? null;
}

// ── email_control ──────────────────────────────────────────────────────────

interface EmailControlFilters {
  status?: string;
  sender?: string;
  days?: number;
  limit?: number;
  hasAttachment?: boolean;
  pdfExtracted?: boolean;
}

// Busca message_ids em financial_account_control pelo invoice_number, depois
// retorna as linhas de email_control correspondentes.
// Usado para permitir pesquisa por nº documento em /emails.
async function lookupEmailsByInvoiceNumber(
  term: string,
  baseParams: QueryParams,
): Promise<EmailControl[]> {
  const u1 = new URL(`${BASE_URL}/rest/v1/financial_account_control`);
  u1.searchParams.set('select', 'gmail_message_id');
  u1.searchParams.set('invoice_number', `ilike.*${term}*`);
  u1.searchParams.set('limit', '100');
  const r1 = await fetch(u1.toString(), { headers: await authHeaders() });
  if (!r1.ok) return [];

  const accts = (await r1.json()) as { gmail_message_id: string | null }[];
  const ids = [
    ...new Set(
      accts
        .map((a) => a.gmail_message_id?.replace(/#\d+$/, ''))
        .filter((id): id is string => Boolean(id)),
    ),
  ];
  if (!ids.length) return [];

  // in.("id1","id2") — aspas duplas para suportar chars especiais no message_id
  const u2 = new URL(`${BASE_URL}/rest/v1/email_control`);
  Object.entries(baseParams).forEach(
    ([k, v]) => v !== undefined && u2.searchParams.set(k, String(v)),
  );
  u2.searchParams.set('message_id', `in.("${ids.join('","')}")`);
  const r2 = await fetch(u2.toString(), { headers: await authHeaders() });
  if (!r2.ok) return [];
  return r2.json() as Promise<EmailControl[]>;
}

export async function getEmailControl({
  status,
  sender,
  days,
  limit = 200,
  hasAttachment,
  pdfExtracted,
}: EmailControlFilters = {}): Promise<EmailControl[]> {
  // Sem offset aqui, mas o desempate mantém a ordem ESTÁVEL entre recargas — senão
  // e-mails de mesmo `received_at` trocam de lugar a cada refresh (ver lib/stableOrder.ts).
  const baseParams: QueryParams = {
    select: '*',
    order: stableOrder({ fallback: 'received_at.desc', tiebreak: 'id' }),
    limit,
  };
  if (status) baseParams['status'] = `eq.${status}`;
  if (days) {
    const since = new Date(Date.now() - days * 86400000).toISOString();
    baseParams['received_at'] = `gte.${since}`;
  }
  if (hasAttachment !== undefined) baseParams['has_attachment'] = `eq.${String(hasAttachment)}`;
  if (pdfExtracted !== undefined) baseParams['pdf_extracted'] = `eq.${String(pdfExtracted)}`;

  if (!sender) return query<EmailControl[]>('email_control', baseParams);

  // Busca em paralelo: por remetente/assunto E por nº documento via lookup inverso
  const senderParams: QueryParams = {
    ...baseParams,
    or: `(sender_email.ilike.*${sender}*,subject.ilike.*${sender}*)`,
  };

  const [senderRows, invoiceRows] = await Promise.all([
    query<EmailControl[]>('email_control', senderParams),
    lookupEmailsByInvoiceNumber(sender, baseParams),
  ]);

  // Merge deduplificado, mantendo ordem desc por received_at
  const seen = new Set(senderRows.map((r) => r.id));
  const merged = [...senderRows];
  for (const r of invoiceRows) {
    if (!seen.has(r.id)) {
      seen.add(r.id);
      merged.push(r);
    }
  }
  return merged.sort((a, b) => (b.received_at ?? '').localeCompare(a.received_at ?? ''));
}

// Marca um e-mail como revisado (reviewed_at = agora) — usado em /emails ao abrir
// o card de detalhes de um e-mail com falha. A migration 030 restringe o papel
// `authenticated` a escrever SOMENTE a coluna reviewed_at. Retorna o ISO gravado.
export async function markEmailReviewed(id: number): Promise<string> {
  const reviewedAt = new Date().toISOString();
  const url = new URL(`${BASE_URL}/rest/v1/email_control`);
  url.searchParams.set('id', `eq.${id}`);
  const res = await fetch(url.toString(), {
    method: 'PATCH',
    headers: await authHeaders({ Prefer: 'return=minimal' }),
    body: JSON.stringify({ reviewed_at: reviewedAt }),
  });
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${await res.text()}`);
  return reviewedAt;
}

// Contagens por status (taxonomia da migration 022) — uma KPI por status.
export interface EmailStats {
  total: number;
  extraido: number;
  recebido: number;
  pendente: number;
  falha: number;
  ignorado: number;
  duplicidade: number;
}

export async function getEmailStats(): Promise<EmailStats> {
  // Uma única requisição: traz a coluna `status` de todo o email_control e conta no
  // cliente (mesmo padrão de getProcessingErrorStats). Antes eram 8 requisições por
  // refresh (1 total + 6 por status), repetidas a cada poll de leitura.
  const rows = await query<{ status: string }[]>('email_control', { select: 'status', limit: 50000 });
  const count = (s: string): number => rows.filter((r) => r.status === s).length;
  return {
    total: rows.length,
    extraido: count('extraído'),
    recebido: count('recebido'),
    pendente: count('pendente'),
    falha: count('falha'),
    ignorado: count('ignorado'),
    duplicidade: count('duplicidade'),
  };
}

// ── financial_account_control ───────────────────────────────────────────────

// SELECT com os JOINs de exibição: fornecedor (nome/CNPJ/CPF) + classificação
// contábil (centro de custo / plano de contas) via aliases de embed do PostgREST.
// Exportado para o teste-guarda de withChartAccountJoin (que precisa provar que a
// promoção a !inner acerta ESTE select, e não um parecido).
// ts-prune-ignore-next
export const SELECT_WITH_EMBEDS =
  '*,supplier(trade_name,legal_name,cnpj,cpf),' +
  // Empresa pagadora (FK sk_company — migrations 083/084): nome exibido na coluna "Empresa"
  // do grid e no card de detalhe. Precisa espelhar o SELECT_WITH_SUPPLIER da Next API — a
  // resposta do PATCH é mesclada IN-PLACE no grid (sem refetch); se só um trouxer o embed,
  // a célula esvazia ao salvar a edição.
  'company(trade_name),' +
  'cost_center:financial_cost_center(cost_center_code,cost_center_description),' +
  // Plano de contas + sua hierarquia (grupo/subgrupo, embeds aninhados) — a célula
  // "Plano de contas" do grid concatena plano + grupo + subgrupo + centro de custo.
  'chart_account:financial_chart_of_account(account_code,account_description,' +
  'group:financial_chart_of_account_group(group_code,group_description),' +
  'subgroup:financial_chart_of_account_subgroup(subgroup_code,subgroup_description)),' +
  // Dimensão `status` (via FK status_id) — fonte do NOME da situação para exibição
  // (a coluna `status` texto está em remoção faseada; status_id é a fonte única).
  'status_dim:status(status_name,status_short_name),' +
  // Anexos (1:N — migration 079): e-mail (origin='pipeline') + upload do usuário ('manual').
  // Espelha o SELECT_WITH_SUPPLIER da Next API (apps/api-backend/lib/contas.ts) — a resposta
  // de POST/PATCH de lá é mesclada IN-PLACE nestas linhas; se um dos dois SELECTs não
  // trouxer os anexos, a lista do detalhe fica inconsistente até o refresh.
  // O soft-deletado já é excluído pela POLICY de SELECT (papel `authenticated`), então aqui
  // não é preciso filtro — diferente da Next API, que usa service_role e ignora a RLS.
  'attachments:financial_account_attachment(id,account_id,storage_key,file_name,mime_type,size_bytes,origin,uploaded_by,created_at)';

interface FinancialAccountControlFilters {
  supplier?: string;
  docType?: string;
  // Situação filtrada por status_id (fonte única). undefined = sem filtro de situação.
  statusId?: number;
  // Empresa pagadora (FK sk_company: 1=OTIMOTEX TECIDOS, 2=LEBIANCO, 3=OTIMOTEX FARDOS, 4=LE BLANC). undefined = todas.
  skCompany?: number;
  paymentMethod?: string;
  // Coluna de data do PERÍODO (botões de mês/ano): vencimento (default) ou emissão.
  dateField?: 'due_date' | 'issue_date';
  // Filtro por mês/ano (0-indexed). Ambos presentes → range do mês na coluna dateField.
  // null/ausente → escopo "todas" (cai no range explícito dateFrom/dateTo, ou nada).
  month?: number | null;
  year?: number | null;
  // Coluna de data do INTERVALO explícito De/Até — INDEPENDENTE de `dateField` (seletor
  // próprio ao lado dos campos). Aceita `payment_date`, que o período não oferece.
  rangeDateField?: 'due_date' | 'issue_date' | 'payment_date';
  dateFrom?: string;
  dateTo?: string;
  // ── Classificação contábil (2ª linha de filtros de /consulta) ────────────────
  // Independentes entre si, combinados por AND. undefined/'' = sem filtro.
  /** FK DIRETA da conta — o centro de custo DA CONTA (o mesmo que o grid exibe). */
  costCenterId?: number;
  /**
   * Plano de contas pela DESCRIÇÃO (é o que o ChartAccountSelect devolve). A mesma
   * descrição existe em vários centros de custo como linhas distintas, então filtrar
   * por ela casa TODOS os planos homônimos — que é a intenção de "filtrar por plano".
   */
  chartAccountDescription?: string;
  /** Coluna de financial_chart_of_account (a fato não tem FK de grupo/subgrupo). */
  chartAccountGroupId?: number;
  chartAccountSubgroupId?: number;
  page?: number;
  pageSize?: number;
  sortCol?: string;
  sortDir?: 'asc' | 'desc';
}

interface Paginated<T> {
  data: T[];
  total: number;
  /** true quando `total` é estimativa (PostgREST não devolveu contagem exata). */
  totalIsEstimate: boolean;
}

// Content-Range do PostgREST: "0-19/247" (count=exact) ou "*/*" / "0-19/*" quando a
// contagem é indisponível. Indisponível → NÃO zera a paginação: estima "há mais páginas"
// por offset + itens recebidos (+ pageSize se a página veio cheia), evitando prender o
// usuário na página 1. Mantém o caminho count=exact intacto.
// ts-prune-ignore-next
export function parsePaginationTotal(
  cr: string | null,
  offset: number,
  pageSize: number,
  dataLength: number,
): { total: number; totalIsEstimate: boolean } {
  const exact = Number.parseInt(cr?.split('/')[1] ?? '', 10);
  if (Number.isFinite(exact)) return { total: exact, totalIsEstimate: false };
  return {
    total: offset + dataLength + (dataLength === pageSize ? pageSize : 0),
    totalIsEstimate: true,
  };
}

// Aplica os filtros de financial_account_control nos search params — compartilhado
// entre a listagem paginada e a soma do card "Valor total".
// Como financial_account_control não guarda mais nome/CNPJ do fornecedor (só a FK
// sk_supplier — migrations 040/041/042), a busca por fornecedor resolve antes os
// sk_supplier que casam o termo na tabela `supplier` (nome, CNPJ/CPF e os 4 e-mails)
// e alimenta o filtro `sk_supplier IN (...)`. Os índices GIN trigram (migration 029)
// aceleram o ILIKE '%termo%' nos e-mails.
// Valor de um `ilike` dentro de um or(...): entre ASPAS DUPLAS para sobreviver a
// caracteres reservados do PostgREST (vírgula e parênteses são delimitadores de
// cláusula). Sem isso, um termo como "463,21" quebra o filtro inteiro (PGRST100).
// As aspas do próprio termo são removidas para não invalidar o literal citado.
function ilikeContains(term: string): string {
  return `"*${term.replace(/"/g, '')}*"`;
}

// Interpreta o termo de busca como VALOR monetário (formato BR ou simples) e devolve
// o número canônico a casar em `amount` (NUMERIC(15,2)); null quando o termo não é um
// valor numérico completo — aí a busca segue apenas textual. A correspondência é EXATA.
// Aceita o símbolo monetário opcional "R$" (com/sem espaço), indicador de busca por valor.
// Exemplos: "463,21" → "463.21" · "R$ 1.999,99" → "1999.99" · "R$1999,99" → "1999.99" · "391".
export function parseBrlAmount(term: string): string | null {
  // Remove o prefixo "R$" (e espaços) antes de validar o número.
  const t = term.trim().replace(/^r\$\s*/i, '').trim();
  let normalized: string;
  if (/^\d{1,3}(\.\d{3})+(,\d{1,2})?$/.test(t)) {
    // BR com separador de milhar: remove os pontos, vírgula → ponto decimal.
    normalized = t.replace(/\./g, '').replace(',', '.');
  } else if (/^\d+,\d{1,2}$/.test(t)) {
    // Vírgula como separador decimal (ex.: "463,21").
    normalized = t.replace(',', '.');
  } else if (/^\d+(\.\d{1,2})?$/.test(t)) {
    // Número simples ou ponto decimal (ex.: "391" ou "463.21").
    normalized = t;
  } else {
    return null;
  }
  const n = Number(normalized);
  return Number.isFinite(n) ? String(n) : null;
}

// O símbolo "R$" no termo indica busca EXPLÍCITA por valor do documento: correspondência
// exata em `amount`, sem busca textual (nº doc/assunto/remetente) nem lookup de fornecedor.
export function isCurrencyValueSearch(term: string): boolean {
  return /r\$/i.test(term) && parseBrlAmount(term) !== null;
}

async function findSupplierIdsByTerm(term: string): Promise<number[]> {
  const url = new URL(`${BASE_URL}/rest/v1/supplier`);
  const like = ilikeContains(term);
  url.searchParams.set('select', 'sk_supplier');
  url.searchParams.set(
    'or',
    `(trade_name.ilike.${like},legal_name.ilike.${like},cnpj.ilike.${like},` +
      `cpf.ilike.${like},email.ilike.${like},email2.ilike.${like},` +
      `email3.ilike.${like},email4.ilike.${like})`,
  );
  url.searchParams.set('limit', '1000');
  const res = await fetch(url.toString(), { headers: await authHeaders() });
  if (!res.ok) return [];
  const data = (await res.json()) as { sk_supplier: number }[];
  return data.map((r) => r.sk_supplier);
}

// GET genérico numa tabela de cadastro que casa `term` (ilike) em `code`/`description`
// e devolve a coluna de id. Espelha findSupplierIdsByTerm para a classificação contábil.
async function findIdsByTerm(
  table: string,
  idCol: string,
  cols: string[],
  term: string,
  extra = '',
): Promise<number[]> {
  const url = new URL(`${BASE_URL}/rest/v1/${table}`);
  const like = ilikeContains(term);
  url.searchParams.set('select', idCol);
  url.searchParams.set('or', `(${cols.map((c) => `${c}.ilike.${like}`).join(',')})`);
  url.searchParams.set('limit', '1000');
  const qs = extra ? `${url.toString()}&${extra}` : url.toString();
  const res = await fetch(qs, { headers: await authHeaders() });
  if (!res.ok) return [];
  const data = (await res.json()) as Record<string, number>[];
  return data.map((r) => r[idCol]);
}

// cost_center_id[] cujo código/descrição casa o termo. Exclui o sentinela id 0 ("não
// informado") — buscar por classificação não deve trazer o balde de não classificados.
async function findCostCenterIdsByTerm(term: string): Promise<number[]> {
  return findIdsByTerm(
    'financial_cost_center', 'cost_center_id',
    ['cost_center_code', 'cost_center_description'], term, 'cost_center_id=gt.0',
  );
}

// chart_account_id[] cujo PLANO (código/descrição), GRUPO ou SUBGRUPO casa o termo. Grupo e
// subgrupo são resolvidos primeiro (FKs diretas do plano: chart_account_group_id migration
// 058, chart_account_subgroup_id) e entram como `.in.(...)` no or do plano.
async function findChartAccountIdsByTerm(term: string): Promise<number[]> {
  const [groupIds, subgroupIds] = await Promise.all([
    findIdsByTerm('financial_chart_of_account_group', 'chart_account_group_id',
      ['group_code', 'group_description'], term),
    findIdsByTerm('financial_chart_of_account_subgroup', 'chart_account_subgroup_id',
      ['subgroup_code', 'subgroup_description'], term),
  ]);
  const url = new URL(`${BASE_URL}/rest/v1/financial_chart_of_account`);
  const like = ilikeContains(term);
  const clauses = [`account_code.ilike.${like}`, `account_description.ilike.${like}`];
  if (groupIds.length) clauses.push(`chart_account_group_id.in.(${groupIds.join(',')})`);
  if (subgroupIds.length) clauses.push(`chart_account_subgroup_id.in.(${subgroupIds.join(',')})`);
  url.searchParams.set('select', 'chart_account_id');
  url.searchParams.set('or', `(${clauses.join(',')})`);
  url.searchParams.set('chart_account_id', 'gt.0');
  url.searchParams.set('limit', '1000');
  const res = await fetch(url.toString(), { headers: await authHeaders() });
  if (!res.ok) return [];
  const data = (await res.json()) as { chart_account_id: number }[];
  return data.map((r) => r.chart_account_id);
}

// IDs de busca resolvidos em paralelo (fornecedor + classificação contábil) para o termo
// livre de /consulta. Busca por valor ("R$ …") pula tudo. Alimenta applyFinancialFilters.
interface SearchIds {
  supplierIds: number[];
  costCenterIds: number[];
  chartAccountIds: number[];
}
async function resolveSearchIds(term: string | undefined): Promise<SearchIds> {
  if (!term || isCurrencyValueSearch(term)) {
    return { supplierIds: [], costCenterIds: [], chartAccountIds: [] };
  }
  const [supplierIds, costCenterIds, chartAccountIds] = await Promise.all([
    findSupplierIdsByTerm(term),
    findCostCenterIdsByTerm(term),
    findChartAccountIdsByTerm(term),
  ]);
  return { supplierIds, costCenterIds, chartAccountIds };
}

const EMPTY_SEARCH_IDS: SearchIds = { supplierIds: [], costCenterIds: [], chartAccountIds: [] };

// ── Planos de contas EM USO (opções do filtro de /consulta) ───────────────────
// O filtro passou a oferecer só as descrições que REALMENTE aparecem em
// financial_account_control. Antes ele listava o cadastro inteiro (611 linhas), e escolher
// um plano sem conta nenhuma devolvia grid vazio — indistinguível de filtro quebrado.
// Medido: 611 planos cadastrados × **84 descrições distintas** de fato em uso.
//
// A consulta é pelo lado do CADASTRO com embed reverso `!inner`, não pelo lado do fato:
//   financial_chart_of_account?select=account_description,financial_account_control!inner(id)
// O `!inner` mantém só os planos com ao menos uma conta VISÍVEL ao usuário — a RLS vale
// dentro do embed, então um usuário de grupo restrito (`sees_only_own_accounts`) recebe
// exatamente as opções que o grid dele consegue mostrar. Por isso a consulta vive AQUI e
// não na Next API: lá a leitura é `service_role`, que ignora RLS e ofereceria ao usuário
// restrito filtros que não produzem linha alguma.
//
// O GRÃO é o que importa para o volume: 85 linhas (teto = as 611 do cadastro) contra 724
// pelo caminho direto no fato, que cresce a cada conta lançada. `financial_account_control
// .limit=1` corta o array de filhos, que não é usado — 7,6 KB no total.
//
// `account_description=not.is.null` descarta o sentinela id 0 ("não informado"): ele tem
// descrição NULL e não é filtrável, coerente com os outros três filtros contábeis.
const USED_CHART_ACCOUNTS_PAGE = 1000;
const USED_CHART_ACCOUNTS_MAX_PAGES = 5;

// ts-prune-ignore-next
export async function listUsedChartAccountDescriptions(): Promise<string[]> {
  const seen = new Set<string>();
  // Pagina mesmo com o teto estrutural em 611: a consulta DECIDE o que o usuário consegue
  // filtrar, e o corte do PostgREST em "Max rows" volta HTTP 200 — sumiria opção sem erro
  // nenhum. É a mesma armadilha já registrada nos scripts de manutenção; aqui ela custa
  // uma condição de laço. O teto de páginas evita laço infinito se o servidor ignorar o
  // offset (mesma defesa de scripts/supabase_rest.py).
  for (let page = 0; page < USED_CHART_ACCOUNTS_MAX_PAGES; page++) {
    const rows = await query<{ account_description: string | null }[]>('financial_chart_of_account', {
      select: 'account_description,financial_account_control!inner(id)',
      'financial_account_control.limit': 1,
      'account_description': 'not.is.null',
      // Desempate pela PK (ver lib/stableOrder.ts): `account_description` NÃO é única — a
      // mesma descrição existe em vários centros de custo, e é justamente por isso que a
      // função deduplica. Sem a PK no `order`, a ordem não é total e a paginação por offset
      // pode PULAR uma linha entre páginas; sendo ela a única portadora de uma descrição, a
      // opção some do filtro sem erro nenhum — a mesma falha silenciosa que a paginação
      // existe para evitar.
      order: stableOrder({ fallback: 'account_description.asc', tiebreak: 'chart_account_id' }),
      limit: USED_CHART_ACCOUNTS_PAGE,
      offset: page * USED_CHART_ACCOUNTS_PAGE,
    });
    for (const r of rows) if (r.account_description) seen.add(r.account_description);
    if (rows.length < USED_CHART_ACCOUNTS_PAGE) break;
  }
  // A mesma descrição existe em vários centros de custo — a dedup é o que transforma as
  // 85 linhas do cadastro nas 84 opções que o usuário vê.
  return [...seen].sort((a, b) => a.localeCompare(b, 'pt-BR'));
}

// ── Filtro por classificação contábil: plano / grupo / subgrupo ────────────────
// Descrição do plano, GRUPO e SUBGRUPO não são colunas de financial_account_control:
// vivem em financial_chart_of_account, alcançada pela FK chart_account_id. O PostgREST
// resolve isso com FILTRO EM RECURSO EMBUTIDO — mas o filtro só DESCARTA A CONTA quando
// o embed é `!inner`.
//
// VERIFICADO CONTRA O BANCO REAL (2026-08-04), porque a falha aqui é SILENCIOSA:
//   embed simples + chart_account.chart_account_group_id=eq.24 → 706 linhas (o TOTAL da
//   tabela — o filtro não restringe nada, e ainda assim responde HTTP 200);
//   com !inner                                                 → 198, que é exatamente
//   o que o SQL equivalente devolve. Sem o !inner a tela mostraria a base inteira como
//   se estivesse filtrada.
//
// O join é COMPROVADAMENTE loss-free: chart_account_id é NOT NULL DEFAULT 0 e a linha
// sentinela id 0 EXISTE no cadastro (migration 048) — medido: 706 contas, 706 após o
// join. Sendo to-one, ele também não pode DUPLICAR linhas, o que quebraria a paginação
// por offset do scroll infinito.
const CHART_ALIAS = 'chart_account';
const CHART_EMBED = `${CHART_ALIAS}:financial_chart_of_account`;
const CHART_EMBED_INNER = `${CHART_EMBED}!inner`;
// Embed MÍNIMO para os SELECTs que não trazem a classificação (KPIs: select=amount / id).
// O PostgREST exige ao menos uma coluna no embed; chart_account_id é a mais barata.
const CHART_EMBED_MINIMAL = `${CHART_EMBED_INNER}(chart_account_id)`;

/**
 * Promove o embed do plano de contas a `!inner` — pré-requisito para filtrar por coluna
 * do recurso embutido. Idempotente. Se o select já embute o plano (o SELECT_WITH_EMBEDS
 * do grid), reescreve no lugar, preservando as colunas e os embeds aninhados
 * (group/subgroup); senão anexa o embed mínimo (caminho dos KPIs).
 *
 * Só é chamado quando há filtro de plano/grupo/subgrupo: sem eles o `select` fica
 * INTOCADO e a URL de abertura da página continua idêntica à de hoje.
 */
// ts-prune-ignore-next
export function withChartAccountJoin(select: string): string {
  if (select.includes(`${CHART_EMBED_INNER}(`)) return select;
  if (select.includes(`${CHART_EMBED}(`)) return select.replace(`${CHART_EMBED}(`, `${CHART_EMBED_INNER}(`);
  // Select ausente/vazio cai em `*` ANTES do embed. Devolver só o embed produziria um
  // `select` sem nenhuma coluna de topo: o PostgREST responderia 200 com linhas contendo
  // apenas `chart_account`, e o grid renderizaria vazio sem erro nenhum. O contrato "o
  // chamador já setou o select" vale para as 3 rotas de hoje, mas contrato escrito só em
  // comentário não protege a 4ª — e a falha dele seria silenciosa, como a do `!inner`.
  return select ? `${select},${CHART_EMBED_MINIMAL}` : `*,${CHART_EMBED_MINIMAL}`;
}

// Exportado para teste unitário puro (sem fetch) — monta o or(...) do PostgREST.
// ts-prune-ignore-next
export function applyFinancialFilters(
  params: URLSearchParams,
  {
    supplier, docType, statusId, skCompany, paymentMethod,
    dateField, month, year, rangeDateField, dateFrom, dateTo,
    costCenterId, chartAccountDescription, chartAccountGroupId, chartAccountSubgroupId,
  }: FinancialAccountControlFilters,
  searchIds: SearchIds = EMPTY_SEARCH_IDS,
  // Só o GRID inclui canceladas; os KPIs (Valor total) mantêm a exclusão para não
  // somar cancelado (evita confusão). Default = excluir cancelado.
  includeCancelled = false,
): void {
  // or= nas colunas próprias da conta (nº documento, assunto, remetente, valor) mais os
  // ids resolvidos pelo termo: sk_supplier (nome/CNPJ/CPF/e-mail do cadastro supplier),
  // cost_center_id (centro de custo) e chart_account_id (plano de contas + grupo/subgrupo).
  if (supplier) {
    if (isCurrencyValueSearch(supplier)) {
      // "R$ ..." → busca EXATA pelo valor do documento, sem busca textual.
      params.set('amount', `eq.${parseBrlAmount(supplier)}`);
    } else {
      const like = ilikeContains(supplier);
      const { supplierIds, costCenterIds, chartAccountIds } = searchIds;
      const clauses = [
        `invoice_number.ilike.${like}`,
        `subject.ilike.${like}`,
        `sender_email.ilike.${like}`,
      ];
      // Termo numérico SEM R$ (ex.: "463,21") também casa o VALOR, além do texto.
      const amount = parseBrlAmount(supplier);
      if (amount) clauses.push(`amount.eq.${amount}`);
      if (supplierIds.length) clauses.push(`sk_supplier.in.(${supplierIds.join(',')})`);
      if (costCenterIds.length) clauses.push(`cost_center_id.in.(${costCenterIds.join(',')})`);
      if (chartAccountIds.length) clauses.push(`chart_account_id.in.(${chartAccountIds.join(',')})`);
      params.set('or', `(${clauses.join(',')})`);
    }
  }
  if (docType) params.set('document_type', `eq.${docType}`);
  // Empresa pagadora — filtro direto pela FK (o embed company(trade_name) é só exibição).
  // Vale para o grid E para os cards "Valor total"/"Total de registros" (que recebem os
  // mesmos filtros); os KPIs gerais (getFinancialStats) são globais por design.
  if (skCompany) params.set('sk_company', `eq.${skCompany}`);
  // Situação (status_id, fonte única): filtro explícito sobrescreve tudo. Sem filtro, o
  // grid mostra TODAS (inclui cancelado); os KPIs mantêm neq.cancelado (por id).
  if (statusId != null) params.set('status_id', `eq.${statusId}`);
  else if (!includeCancelled) params.set('status_id', `neq.${STATUS_ID_CANCELADO}`);
  if (paymentMethod) params.set('payment_method', `eq.${paymentMethod}`);
  // Centro de custo — FK DIRETA da conta, sem join. `!= null` (e não truthy) porque 0 é
  // o sentinela "não informado", um id legítimo: truthy o descartaria em silêncio.
  if (costCenterId != null) params.set('cost_center_id', `eq.${costCenterId}`);
  // Plano / grupo / subgrupo — filtro no recurso EMBUTIDO (ver withChartAccountJoin).
  // NÃO competem pelo ÚNICO slot `or=` (que é da busca livre): são params escalares em
  // chaves próprias, que o PostgREST combina por AND com tudo o mais — inclusive com o
  // próprio or= e com o status_id=neq.cancelado dos KPIs.
  //
  // O valor de `eq.` vai CRU, sem aspas. Medido nesta instalação: `eq."Serviços Gerais"`
  // devolve 0 linhas (as aspas entram no valor comparado) enquanto `eq.Serviços Gerais`
  // devolve as 2 corretas — e o mesmo vale para descrição com vírgula ou parênteses, que
  // existem no cadastro (9 planos). Aspas só são interpretadas DENTRO de lista (`or=`,
  // `in.()`), que é o caso do ilikeContains logo acima — não em parâmetro isolado.
  const chartFilters: [string, string][] = [];
  if (chartAccountDescription) chartFilters.push([`${CHART_ALIAS}.account_description`, `eq.${chartAccountDescription}`]);
  if (chartAccountGroupId != null) chartFilters.push([`${CHART_ALIAS}.chart_account_group_id`, `eq.${chartAccountGroupId}`]);
  if (chartAccountSubgroupId != null) chartFilters.push([`${CHART_ALIAS}.chart_account_subgroup_id`, `eq.${chartAccountSubgroupId}`]);
  if (chartFilters.length > 0) {
    // CONTRATO: o chamador JÁ setou `select` (as 3 rotas fazem: grid, valor total e
    // contagem). Sem promover a !inner o filtro não descarta conta nenhuma — ver o
    // bloco de verificação em CHART_EMBED.
    params.set('select', withChartAccountJoin(params.get('select') ?? ''));
    for (const [key, value] of chartFilters) params.set(key, value);
  }
  // Filtro de data, em DOIS ramos mutuamente exclusivos e com colunas INDEPENDENTES:
  //  1) Intervalo explícito dateFrom/dateTo (seletor próprio ao lado dos campos De/Até,
  //     OU card "7 dias") tem PRECEDÊNCIA — quando presente, vence o mês/ano.
  //  2) Senão, mês/ano (botões de período) monta o range do mês [01, último dia] na
  //     coluna do seletor da linha dos meses.
  //  3) Sem nada, não filtra por data.
  //
  // As duas colunas são separadas de propósito: o usuário pode navegar o período por
  // vencimento e, ao mesmo tempo, pesquisar um intervalo por data de pagamento. NÃO
  // colapsar num `rangeDateField ?? dateField` — é o mutante que preserva todas as
  // referências, passa em typecheck/lint e só se manifesta quando os dois divergem
  // (guardas em supabase.test.ts, "coluna de data do intervalo × do período").
  const rangeCol = rangeDateField ?? 'due_date';
  const periodCol = dateField ?? 'due_date';
  if (dateFrom || dateTo) {
    if (dateFrom) params.append(rangeCol, `gte.${dateFrom}`);
    if (dateTo) params.append(rangeCol, `lte.${dateTo}`);
  } else if (month != null && year != null) {
    const first = new Date(Date.UTC(year, month, 1)).toISOString().slice(0, 10);
    const last = new Date(Date.UTC(year, month + 1, 0)).toISOString().slice(0, 10);
    params.append(periodCol, `gte.${first}`);
    params.append(periodCol, `lte.${last}`);
  }
}

// Recebe o objeto de filtros INTEIRO e o repassa a applyFinancialFilters — mesmo padrão
// de getFinancialAccountTotalValue/getFinancialAccountCount. Antes esta função
// destrinchava os 14 campos e REMONTAVA um literal para a chamada; um campo novo que
// entrasse só na interface era descartado aqui em silêncio (sem erro de tipo, sem teste
// vermelho) enquanto os KPIs o respeitavam — o sintoma seria "o grid está errado", longe
// da causa. Só paginação/ordenação são desestruturadas, porque são usadas aqui mesmo;
// que elas viajem junto para applyFinancialFilters é inócuo (ela lê só o que lhe cabe).
export async function getFinancialAccountControl(
  filters: FinancialAccountControlFilters = {},
): Promise<Paginated<FinancialAccountControl>> {
  const { supplier, page = 1, pageSize = 20, sortCol, sortDir } = filters;
  const offset = (page - 1) * pageSize;
  const url = new URL(`${BASE_URL}/rest/v1/financial_account_control`);
  // JOIN com supplier via FK sk_supplier — retorna dados canônicos do cadastro
  // (nome/CNPJ/CPF). Fonte de verdade única; não há mais colunas denormalizadas.
  // Embeds de classificação contábil (FKs cost_center_id / chart_account_id).
  url.searchParams.set('select', SELECT_WITH_EMBEDS);
  // Ordenação padrão = criação (created_at) descendente — mais recente no topo (igual ao /emails).
  // Sort explícito do usuário sobrescreve. A coluna "Situação" ordena pelo NOME da
  // dimensão (alfabético — decisão de negócio; id ≠ ordem), via order=status_dim(status_name).
  // O desempate por `id` (stableOrder) é OBRIGATÓRIO: sem ele, empates + paginação por
  // offset fazem a mesma conta aparecer duas vezes no scroll infinito e OUTRA sumir da
  // tela. Ordenar por Situação empata 682 de 682 linhas — ver lib/stableOrder.ts.
  const orderCol = sortCol === STATUS_SORT_KEY ? STATUS_DIM_ORDER : sortCol;
  url.searchParams.set(
    'order',
    stableOrder({ column: orderCol, dir: sortDir, fallback: 'created_at.desc', tiebreak: 'id' }),
  );
  url.searchParams.set('limit', String(pageSize));
  url.searchParams.set('offset', String(offset));
  // Busca "R$ ..." é por valor → não resolve ids pelo termo. Senão, resolve fornecedor +
  // classificação contábil (centro de custo / plano de contas / grupo / subgrupo) em paralelo.
  const searchIds = await resolveSearchIds(supplier);
  // Grid: inclui canceladas (includeCancelled=true). Os KPIs continuam excluindo.
  applyFinancialFilters(url.searchParams, filters, searchIds, true);
  const reqHeaders = await authHeaders({ Prefer: 'count=exact' });
  const res = await fetch(url.toString(), { headers: reqHeaders });
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as FinancialAccountControl[];
  const cr = res.headers.get('Content-Range');
  const { total, totalIsEstimate } = parsePaginationTotal(cr, offset, pageSize, data.length);
  return { data, total, totalIsEstimate };
}

// Diretório id→e-mail dos usuários (view app_user, migration 077) para exibir o AUTOR
// (criado / editado / situação alterada) no detalhe de /consulta. São poucos usuários
// internos → busca única, sem paginação. Falha não é crítica (o detalhe cai no fallback).
export async function getAppUsers(): Promise<Record<string, string>> {
  const url = new URL(`${BASE_URL}/rest/v1/app_user`);
  url.searchParams.set('select', 'id,email');
  const res = await fetch(url.toString(), { headers: await authHeaders() });
  if (!res.ok) return {};
  const rows = (await res.json()) as { id: string; email: string }[];
  return Object.fromEntries(rows.map((r) => [r.id, r.email]));
}

// Flags de curadoria manual de uma conta ("Tem NF ?" / "Tem Boleto"), editáveis
// como checkbox em /consulta. A migration 033 restringe o papel `authenticated`
// a escrever SOMENTE estas duas colunas (column-level grant + policy de RLS).
export type FinancialAccountFlag = 'has_invoice' | 'has_bank_slip';

export async function setFinancialAccountFlag(
  id: number,
  field: FinancialAccountFlag,
  value: boolean,
): Promise<void> {
  const url = new URL(`${BASE_URL}/rest/v1/financial_account_control`);
  url.searchParams.set('id', `eq.${id}`);
  const res = await fetch(url.toString(), {
    method: 'PATCH',
    headers: await authHeaders({ Prefer: 'return=minimal' }),
    body: JSON.stringify({ [field]: value }),
  });
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${await res.text()}`);
}

// Permite ao usuário autenticado alterar a situação de uma conta em /consulta — grava
// por status_id (fonte única). A migration 068 concede GRANT UPDATE (status_id) TO
// authenticated; a trigger id-primária (SECURITY DEFINER) deriva o texto `status`.
export async function setFinancialAccountStatus(id: number, statusId: number): Promise<void> {
  const url = new URL(`${BASE_URL}/rest/v1/financial_account_control`);
  url.searchParams.set('id', `eq.${id}`);
  const res = await fetch(url.toString(), {
    method: 'PATCH',
    headers: await authHeaders({ Prefer: 'return=minimal' }),
    body: JSON.stringify({ status_id: statusId }),
  });
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${await res.text()}`);
}

export async function setFinancialAccountStatusBulk(ids: number[], statusId: number): Promise<void> {
  const url = new URL(`${BASE_URL}/rest/v1/financial_account_control`);
  url.searchParams.set('id', `in.(${ids.join(',')})`);
  const res = await fetch(url.toString(), {
    method: 'PATCH',
    headers: await authHeaders({ Prefer: 'return=minimal' }),
    body: JSON.stringify({ status_id: statusId }),
  });
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${await res.text()}`);
}

// `getFinancialAccountTotalValue` e `getFinancialAccountCount` foram REMOVIDAS em
// 2026-08-08: com `getFinancialStats(filters)` devolvendo contagem e soma já filtradas, os
// mesmos números passariam a vir de TRÊS consultas por apply — três fontes que precisam
// concordar e uma requisição a mais para cada. Pior, era dessa divisão que nascia o defeito
// relatado: o card "Total de registros" tirava o VALOR da consulta filtrada e a CONTAGEM
// dos KPIs globais, e mostrava "R$ 3,7 mi / 742 contas" quando agosto tinha 211.
// A soma por agregação no servidor (`sum()` do PostgREST) não é alternativa aqui:
// `db-aggregates-enabled` vem desligado no Supabase — ver CLAUDE.md, decisão 2 da Fase 0.

// ── email_processing_errors ───────────────────────────────────────────────

interface ProcessingErrorFilters {
  errorType?: string;
  sender?: string;
  dateFrom?: string;
  dateTo?: string;
  page?: number;
  pageSize?: number;
}

export async function getProcessingErrors({
  errorType,
  sender,
  dateFrom,
  dateTo,
  page = 1,
  pageSize = 25,
}: ProcessingErrorFilters = {}): Promise<Paginated<ProcessingError>> {
  const offset = (page - 1) * pageSize;
  const url = new URL(`${BASE_URL}/rest/v1/email_processing_errors`);
  url.searchParams.set('select', '*');
  // Desempate obrigatório — paginação por offset (ver lib/stableOrder.ts).
  url.searchParams.set('order', stableOrder({ fallback: 'logged_at.desc', tiebreak: 'id' }));
  url.searchParams.set('limit', String(pageSize));
  url.searchParams.set('offset', String(offset));
  if (errorType) url.searchParams.set('error_type', `eq.${errorType}`);
  if (sender) url.searchParams.set('sender_email', `ilike.*${sender}*`);
  if (dateFrom) url.searchParams.append('logged_at', `gte.${dateFrom}`);
  if (dateTo) url.searchParams.append('logged_at', `lte.${dateTo}T23:59:59`);
  const reqHeaders = await authHeaders({ Prefer: 'count=exact' });
  const res = await fetch(url.toString(), { headers: reqHeaders });
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as ProcessingError[];
  const cr = res.headers.get('Content-Range');
  const { total, totalIsEstimate } = parsePaginationTotal(cr, offset, pageSize, data.length);
  return { data, total, totalIsEstimate };
}

export interface ProcessingErrorStats {
  total: number;
  counts: Record<string, number>;
}

export async function getProcessingErrorStats(): Promise<ProcessingErrorStats> {
  const data = await query<{ error_type: string }[]>('email_processing_errors', {
    select: 'error_type',
    limit: 5000,
  });
  const counts: Record<string, number> = {};
  for (const r of data) counts[r.error_type] = (counts[r.error_type] || 0) + 1;
  return { total: data.length, counts };
}

// Conta(s) extraida(s) ligada(s) a um e-mail, via gmail_message_id.
// Multiplos PDFs recebem sufixo (#1, #2), por isso o filtro usa LIKE prefixo.
export async function getAccountsByMessageId(messageId: string | null): Promise<FinancialAccountControl[]> {
  if (!messageId) return [];
  return query<FinancialAccountControl[]>('financial_account_control', {
    select: SELECT_WITH_EMBEDS,
    gmail_message_id: `like.${messageId}*`,
    order: 'due_date.asc',
  });
}

// Busca em lote o primeiro invoice_number de cada message_id.
// Usa PostgREST `or=(like.id*)` para cobrir o sufixo #N de múltiplos PDFs.
// Chunkeia em grupos de 50 para evitar URL muito longa.
export async function getInvoiceNumbersByMessageIds(
  messageIds: string[],
): Promise<Record<string, string>> {
  if (!messageIds.length) return {};
  const CHUNK = 50;
  const map: Record<string, string> = {};
  for (let i = 0; i < messageIds.length; i += CHUNK) {
    const chunk = messageIds.slice(i, i + CHUNK);
    const orVal = `(${chunk.map((id) => `gmail_message_id.like.${id}*`).join(',')})`;
    const url = new URL(`${BASE_URL}/rest/v1/financial_account_control`);
    url.searchParams.set('select', 'gmail_message_id,invoice_number');
    url.searchParams.set('or', orVal);
    url.searchParams.set('limit', '500');
    const res = await fetch(url.toString(), { headers: await authHeaders() });
    if (!res.ok) continue;
    const data = (await res.json()) as { gmail_message_id: string; invoice_number: string | null }[];
    for (const r of data) {
      if (!r.invoice_number) continue;
      const base = r.gmail_message_id.replace(/#\d+$/, '');
      if (!map[base]) map[base] = r.invoice_number;
    }
  }
  return map;
}

export interface FinancialStats {
  totalRecords: number;
  totalValue: number;
  pago: number;
  pagoValue: number;
  aVencer: number;
  aVencerValue: number;
  vencendo: number;
  vencendoValue: number;
  vencidas: number;
  vencidasValue: number;
}

type StatsRow = Pick<FinancialAccountControl, 'amount' | 'status_id' | 'due_date'>;

// Página da varredura dos KPIs. Buscar "tudo de uma vez" NÃO existe nesta instalação: o
// PostgREST corta no "Max rows" (1.000) e devolve HTTP 200 — medido por HTTP em
// 2026-08-08 contra `email_control` (1.303 linhas): `limit=1000`, `5000` e `10000`
// devolvem 1.000, sempre 200, sem erro e sem sinal. A versão anterior pedia `limit: 1000`
// e contava `all.length`, então passaria a subnotificar os CINCO cards assim que a base
// crescesse — silenciosamente. Mesma lição da Onda 3 (ver CLAUDE.md).
const STATS_PAGE_SIZE = 1000;
// Teto de páginas: servidor que ignore `offset` faria o laço girar para sempre. Levanta em
// vez de travar — 200 páginas = 200 mil contas, folga de anos sobre as ~22 contas/dia
// medidas em 2026-08.
const STATS_MAX_PAGES = 200;

// Agregação dos 5 KPIs sobre as linhas já carregadas. Separada da busca para ser testável
// sem rede. `todayISO`/`isoDaysFromToday` são LOCAIS de propósito: com `toISOString()`
// (UTC) a janela de 7 dias andava um dia à noite, em UTC−3 — ver lib/format.ts.
function aggregateFinancialStats(all: StatsRow[]): FinancialStats {
  const sum = (rows: StatsRow[]) => rows.reduce((s, r) => s + (Number(r.amount) || 0), 0);
  const hoje = todayISO();
  const in7 = isoDaysFromToday(7);

  const pagoRows = all.filter((r) => r.status_id === STATUS_ID_PAGO);
  const aVencerRows = all.filter((r) => r.status_id === STATUS_ID_A_VENCER);
  const vencendoRows = aVencerRows.filter(
    (r) => r.due_date !== null && r.due_date >= hoje && r.due_date <= in7,
  );
  const vencidasRows = all.filter((r) => r.status_id === STATUS_ID_VENCIDO);

  return {
    totalRecords: all.length,
    totalValue: sum(all),
    pago: pagoRows.length,
    pagoValue: sum(pagoRows),
    aVencer: aVencerRows.length,
    aVencerValue: sum(aVencerRows),
    vencendo: vencendoRows.length,
    vencendoValue: sum(vencendoRows),
    vencidas: vencidasRows.length,
    vencidasValue: sum(vencidasRows),
  };
}

/**
 * KPIs de /consulta para o filtro corrente. Recebe o objeto de filtros INTEIRO e o repassa
 * a `applyFinancialFilters` — mesmo padrão de `getFinancialAccountControl`: destrinchar
 * campo a campo faria um filtro novo ser descartado aqui em silêncio enquanto o grid o
 * respeitasse, e o sintoma ("os KPIs estão errados") apareceria longe da causa.
 *
 * Sem argumento, devolve os KPIs globais (o comportamento anterior).
 * Cancelado fica de fora por padrão (`includeCancelled` default false), como no "Valor
 * total"; filtro explícito de situação sobrepõe.
 */
export async function getFinancialStats(
  filters: FinancialAccountControlFilters = {},
): Promise<FinancialStats> {
  const searchIds = await resolveSearchIds(filters.supplier);
  const all: StatsRow[] = [];
  for (let pagina = 0; pagina < STATS_MAX_PAGES; pagina++) {
    const url = new URL(`${BASE_URL}/rest/v1/financial_account_control`);
    url.searchParams.set('select', 'amount,status_id,due_date');
    // ORDER determinístico é OBRIGATÓRIO com paginação por offset: sem ele o PostgREST não
    // garante a mesma ordem entre requisições e o offset PULA linha (a mesma causa do
    // scroll infinito duplicando conta — ver lib/stableOrder.ts).
    url.searchParams.set('order', 'id.asc');
    url.searchParams.set('limit', String(STATS_PAGE_SIZE));
    url.searchParams.set('offset', String(pagina * STATS_PAGE_SIZE));
    // Depois do `select` (o contrato de withChartAccountJoin) e antes do fetch.
    applyFinancialFilters(url.searchParams, filters, searchIds);
    const res = await fetch(url.toString(), { headers: await authHeaders() });
    if (!res.ok) throw new Error(`Supabase ${res.status}: ${await res.text()}`);
    const lote = (await res.json()) as StatsRow[];
    all.push(...lote);
    if (lote.length < STATS_PAGE_SIZE) return aggregateFinancialStats(all);
  }
  throw new Error(
    `getFinancialStats: paginação não convergiu em ${STATS_MAX_PAGES} páginas — ` +
      'o servidor pode estar ignorando o offset',
  );
}

// ============================================================================
//  ADICIONAR AO FINAL DE: apps/frontend-vite/src/services/supabase.ts
//  (usa os helpers privados `query`/`authHeaders`/`BASE_URL` já existentes
//   no arquivo — por isso este bloco precisa viver DENTRO de supabase.ts.)
// ============================================================================

// ── dashboard ────────────────────────────────────────────────────────────────
// Agrega tudo que o Dashboard financeiro precisa em 2 leituras: as contas do mês
// selecionado (KPIs, situação por status, ranking de fornecedores, prioritárias)
// e as contas do ano (movimentações mês a mês). Tudo calculado no cliente, no
// mesmo estilo de getFinancialStats. `month` é 0-indexed (0 = Janeiro).

export interface DashboardKpis {
  totalCount: number; totalValue: number;
  pagoCount: number; pagoValue: number;
  aVencerCount: number; aVencerValue: number;
  vencendoCount: number; vencendoValue: number; // a vencer em 7 dias
  vencidasCount: number; vencidasValue: number;
}
export interface StatusSlice { status: string; count: number; value: number }
// Fatia genérica de donut por rótulo (tipos de conta / formas de pagamento).
export interface LabelSlice { label: string; count: number; value: number }
// `key` = a identidade do balde (RankPick.key: `cc:<id>`/`sg:<id>`/`sup:<nome>`/`∅`). É o que
// o drill-down usa para reproduzir EXATAMENTE as contas daquela linha do ranking (ver
// filterExpenseDetailRows) — nunca o `name`, que pode ser homônimo/prefixado por código.
// `pct` (opcional) = % do TOTAL de registros do escopo (não da soma dos valores exibidos no
// top-N) que esta linha representa — só `rankBy` (dashboard financeiro) o preenche; o
// ranking de fornecedores (vencimentos) não, e o RankingList mostra `count` nesse caso.
export interface SupplierRank { key: string; name: string; value: number; count: number; pct?: number }
export interface MonthlyFlow { month: number; aPagar: number; pago: number }
export type PriorityKind = 'agua' | 'luz' | 'internet' | 'telefone' | 'aluguel' | 'tributo' | 'outro';
export interface PriorityAccount {
  id: number; kind: PriorityKind; supplier: string;
  due: string | null; amount: number | null; status: string; critical: boolean;
}
export type DashboardScope = 'month' | 'all';
// Teto das leituras dos dashboards (o PostgREST exige um limit). Nomeados para os dois
// dashboards não divergirem e para a guarda de truncagem comparar contra o MESMO número.
const MONTH_READ_LIMIT = 5000;
const ALL_READ_LIMIT = 20000;
// Filtro por KPI clicado no topo do Dashboard. 'total' = sem filtro (padrão).
// Os cards de KPI seguem mostrando os totais completos; só os gráficos filtram.
export type KpiFilter = 'total' | 'pago' | 'aVencer' | 'vencendo7' | 'vencidas';
export interface DashboardData {
  month: number; year: number; scope: DashboardScope;
  kpis: DashboardKpis;
  statusBreakdown: StatusSlice[];
  documentTypeBreakdown: LabelSlice[];
  taxTypeBreakdown: LabelSlice[];
  paymentMethodBreakdown: LabelSlice[];
  supplierRanking: SupplierRank[];
  monthlyFlow: MonthlyFlow[];
  priorityAccounts: PriorityAccount[];
}

// Classificação de contas prioritárias/essenciais por palavra-chave no nome do
// fornecedor + descrição + tipo de documento. Ajuste as regexes conforme os
// fornecedores reais da operação.
const PRIORITY_RULES: { kind: PriorityKind; re: RegExp }[] = [
  { kind: 'agua', re: /\b(sabesp|[áa]guas?|saneamento|copasa|caesb|sanepar|aegea|cedae)\b/i },
  { kind: 'luz', re: /\b(cpfl|energia|el[ée]tric|enel|cemig|light|celesc|coelba|equatorial|neoenergia|elektro|edp)\b/i },
  { kind: 'internet', re: /\b(vivo|claro|tim\b|oi\b|net\b|fibra|internet|telecom|algar|brisanet|telef[oô]nica)\b/i },
  { kind: 'telefone', re: /\b(telefon[ei]a?|celular|m[óo]vel)\b/i },
  { kind: 'aluguel', re: /\b(aluguel|alug[uú][ée]is|imobili[áa]ri|loca[çc][ãa]o|condom[íi]nio)\b/i },
  { kind: 'tributo', re: /\b(darf|das\b|gps\b|inss|fgts|iss\b|icms|tribut|imposto|receita federal|prefeitura|sefaz)\b/i },
];

function classifyPriority(text: string): PriorityKind | null {
  for (const r of PRIORITY_RULES) if (r.re.test(text)) return r.kind;
  return null;
}

type MonthRow = Pick<FinancialAccountControl, 'id' | 'amount' | 'status_id' | 'due_date' | 'document_type' | 'payment_method' | 'description'> & {
  supplier?: { trade_name: string | null; legal_name: string | null } | null;
};
type YearRow = Pick<FinancialAccountControl, 'amount' | 'status_id' | 'due_date'>;

const num = (v: number | null | undefined): number => Number(v) || 0;
const supplierName = (r: MonthRow): string => r.supplier?.trade_name ?? r.supplier?.legal_name ?? 'Sem fornecedor';

/**
 * Sinaliza leitura TRUNCADA pelo `limit` do PostgREST.
 *
 * Os dashboards agregam no cliente sobre um read com teto fixo. Batido o teto, o KPI e o
 * gráfico ficam ERRADOS sem nenhum sinal na tela — a falha mais perigosa aqui é a
 * silenciosa. Não dá para "consertar" no cliente (exigiria paginar ou agregar no banco),
 * mas registrar deixa a causa rastreável quando os números não fecharem. Rows == limit é
 * fortíssimo indício de corte (empate exato é possível, daí o texto "possivelmente").
 */
function warnIfTruncated(rows: readonly unknown[], limit: number, label: string): void {
  if (rows.length >= limit) {
    console.warn(
      `[dashboard] leitura "${label}" possivelmente TRUNCADA em ${limit} linhas — ` +
        'os totais podem estar subestimados. Aumentar o limite ou agregar no banco.',
    );
  }
}

// Top-N (por VALOR — R$) de fatias PRÓPRIAS num donut antes da fatia sintética "outros".
// Fonte ÚNICA: usada como default de `breakdownBy` E de `topBucketLabels`/`matchDonutBucket`
// (drill-down) — se um dia mudar, os dois lados mudam juntos e a fatia/detalhe não divergem.
// = 6 para o donut nunca exibir mais que 7 LINHAS no total (6 categorias reais + a fatia
// sintética "outros", quando houver sobra — decisão do usuário 2026-07-22: "outros" CONTA
// como um dos 7 itens).
const DONUT_TOP_N = 6;

// Agrega linhas por um campo de rótulo → Top N por VALOR (R$) + fatia "outros".
// Rótulo ausente (null) vira "não informado". Genérica sobre o tipo de linha (basta ter
// `amount`) — serve tanto MonthRow (vencimentos) quanto ExpenseMonthRow (financeiro).
function breakdownBy<T extends { amount: number | null }>(rows: T[], pick: (r: T) => string | null, topN = DONUT_TOP_N): LabelSlice[] {
  const map = new Map<string, { count: number; value: number }>();
  for (const r of rows) {
    const k = pick(r) ?? 'não informado';
    const cur = map.get(k) ?? { count: 0, value: 0 };
    cur.count += 1; cur.value += num(r.amount);
    map.set(k, cur);
  }
  // Top-N pela MESMA seleção que o drill-down usa (topBucketLabels) — assim a fatia e o
  // detalhe nunca divergem. A ordem de saída é irrelevante (o BreakdownDonut reordena por
  // VALOR); rótulo fora do top-N é somado na fatia sintética "outros".
  const top = topBucketLabels(rows, pick, topN);
  const result: LabelSlice[] = [];
  const rest = { count: 0, value: 0 };
  for (const [label, v] of map) {
    if (top.has(label)) result.push({ label, ...v });
    else { rest.count += v.count; rest.value += v.value; }
  }
  if (rest.count > 0) result.push({ label: 'outros', ...rest });
  return result;
}

// Conjunto de rótulos que viram fatia PRÓPRIA no `breakdownBy` (Top-N por VALOR — R$ —
// somado por rótulo, mesmo critério `sum(amount) desc → slice(0,topN)`); um rótulo fora
// deste conjunto caiu na fatia sintética "outros". Fonte da verdade do balde compartilhada
// entre o donut e o matcher do drill-down (filterExpenseDetailRows), para o detalhe
// reproduzir a fatia sem divergir. Guarda de não-divergência com `breakdownBy` em
// supabase.drill.test.ts.
//
// POR QUE VALOR, NÃO CONTAGEM (não regredir — bug real corrigido em 2026-07-22): o donut
// exibe arco/%/ordem por VALOR, então a seleção do top-N precisa usar o MESMO critério —
// senão um grupo com POUCAS contas de valor ALTO (ex.: "Serviços Gerais": 2 contas, R$20 mil)
// perde para um grupo com MUITAS contas de valor BAIXO (ex.: "Despesas com Utilidades": 5
// contas, R$8 mil) e cai em "outros" apesar de valer mais — dado financeiro relevante
// escondido atrás de ruído. Caso de origem: conta da PANIFICADORA BELGA (fornecedor,
// R$20.100,80, grupo "Serviços Gerais", subgrupo "Copa e Cozinha" — corretamente classificado
// como Despesas Fixas) aparecia em "outros" do donut "Despesas Fixas" só porque o grupo tinha
// poucas contas, não por erro de relacionamento/join (verificado: FK direta e FK via subgrupo
// do plano de contas eram consistentes, ambas apontando para o mesmo grupo).
export function topBucketLabels<T extends { amount: number | null }>(
  rows: T[], pick: (r: T) => string | null, topN = DONUT_TOP_N,
): Set<string> {
  const valueByLabel = new Map<string, number>();
  for (const r of rows) {
    const k = pick(r) ?? 'não informado';
    valueByLabel.set(k, (valueByLabel.get(k) ?? 0) + num(r.amount));
  }
  if (valueByLabel.size <= topN) return new Set(valueByLabel.keys());
  return new Set(
    [...valueByLabel.entries()].sort((a, b) => b[1] - a[1]).slice(0, topN).map(([label]) => label),
  );
}

// Documentos tributários (guias de arrecadação) — agrupados num único rótulo
// "Tributos" no donut de tipos de conta. Espelha a noção de documento fiscal do
// pipeline (_is_tax_document); `gps` (INSS/previdência) incluído por ser guia de
// arrecadação. Ajuste o conjunto se a operação exigir.
// 🔴 Esta lista é a 4ª cópia da noção de "tributo" no sistema e, ao contrário das três
// do pipeline, não pode ser derivada do enum (nem todo `document_type` é tributo). A
// paridade é travada por `FrontendTaxSetTest` em tests/test_doc_type_domain_consistency.py:
// tipo tributário novo aqui esquecido cai no balde "outros" do donut, sem erro nenhum.
const TAX_DOCUMENT_TYPES = new Set<string>([
  'darf', 'gps', 'das', 'gru', 'dae', 'dar / dare', 'gnre',
  'ipva', 'iptu', 'dam / duam', 'iss', 'itbi', 'gare', 'tributo',
]);
export function isTaxDocumentType(dt: string | null): boolean {
  return !!dt && TAX_DOCUMENT_TYPES.has(dt.toLowerCase());
}
// No donut de tipos de conta, todas as guias tributárias colapsam em "Tributos"
// (o detalhamento por tipo vai no donut dedicado "Tributos").
export function groupDocumentTypeLabel(dt: string | null): string | null {
  return isTaxDocumentType(dt) ? 'Tributos' : dt;
}

// Predicado do filtro por KPI. Vale para qualquer linha com status_id + due_date
// (MonthRow e YearRow). 'total' não filtra.
function matchesKpiFilter(
  r: { status_id: number; due_date: string | null },
  filter: KpiFilter,
  todayStr: string,
  in7: string,
): boolean {
  switch (filter) {
    case 'pago': return r.status_id === STATUS_ID_PAGO;
    case 'aVencer': return r.status_id === STATUS_ID_A_VENCER;
    case 'vencendo7': return r.status_id === STATUS_ID_A_VENCER && !!r.due_date && r.due_date >= todayStr && r.due_date <= in7;
    case 'vencidas': return r.status_id === STATUS_ID_VENCIDO;
    default: return true; // 'total'
  }
}

/**
 * Janela de datas dos dashboards, em ISO (YYYY-MM-DD), como o PostgREST espera.
 *
 * `first`/`last` delimitam o mes pedido; `todayStr`/`in7` sao o "hoje" e o "hoje + 7 dias"
 * usados pelos KPIs de vencimento. Os dois dashboards compartilham ESTA definicao — antes
 * cada um tinha a sua copia, e um ajuste de borda em um deles nao chegaria ao outro.
 *
 * O mes usa UTC (a coluna do banco e `date`, sem hora, entao o fuso local deslocaria a
 * borda) — `first`/`last` devem CONTINUAR em `Date.UTC`.
 *
 * 🔴 `todayStr`/`in7` sao LOCAIS (`isoDaysFromToday`), nunca `toISOString()`. Ate 2026-08-15
 * esta funcao derivava os dois em UTC e, em UTC−3, das ~21h a meia-noite a janela inteira
 * deslizava um dia: a conta que vence HOJE saia do KPI "a vencer em 7 dias" e a de hoje+8
 * entrava. So o irmao `aggregateFinancialStats` (de /consulta) tinha sido corrigido na
 * varredura de 2026-08-08, e as duas telas passavam a responder numeros diferentes para a
 * MESMA pergunta na janela noturna. Medido no dia da correcao: 72 contas na janela, 7
 * vencendo no proprio dia — as 7 sumiam. No ULTIMO dia do mes era pior: a janela caia
 * inteira no mes seguinte, sem interseccao com `first`/`last`, e o dashboard abria vazio.
 * Regra do CLAUDE.md: "TODA data derivada de hoje passa por `isoDaysFromToday` — inclusive
 * as JANELAS". Borda travada em `services/dashboard.test.ts`.
 */
function dashboardWindow(
  month: number,
  year: number,
): { first: string; last: string; todayStr: string; in7: string } {
  const iso = (d: Date): string => d.toISOString().slice(0, 10);
  return {
    first: iso(new Date(Date.UTC(year, month, 1))),
    last: iso(new Date(Date.UTC(year, month + 1, 0))), // dia 0 do mes seguinte = ultimo do mes
    todayStr: isoDaysFromToday(0),
    in7: isoDaysFromToday(7),
  };
}

// Teto da leitura conforme o escopo — fonte unica para o `limit` da query E para a guarda
// de truncagem, que precisam comparar contra o MESMO numero.
const readLimit = (scope: DashboardScope): number => (scope === 'month' ? MONTH_READ_LIMIT : ALL_READ_LIMIT);

/**
 * Os 5 KPIs dos dashboards a partir das linhas do escopo.
 *
 * Era um bloco DUPLICADO literalmente nos dois dashboards. Sao indicadores financeiros: com
 * duas copias, corrigir a regra de "a vencer em 7 dias" numa tela e nao na outra faria as
 * duas mostrarem numeros diferentes para a mesma pergunta, sem erro visivel. Generica sobre
 * o tipo da linha (basta ter valor, situacao e vencimento).
 *
 * O conjunto aqui e sempre o COMPLETO do escopo: o filtro de KPI clicado afeta so os
 * graficos, nunca os cards (senao clicar num card zeraria os demais).
 */
function computeKpis<T extends { amount: number | null; status_id: number; due_date: string | null }>(
  rows: T[],
  todayStr: string,
  in7: string,
): DashboardKpis {
  const sum = (rs: T[]): number => rs.reduce((acc, r) => acc + num(r.amount), 0);
  const pagoRows = rows.filter((r) => r.status_id === STATUS_ID_PAGO);
  const aVencerRows = rows.filter((r) => r.status_id === STATUS_ID_A_VENCER);
  const vencendoRows = aVencerRows.filter((r) => r.due_date && r.due_date >= todayStr && r.due_date <= in7);
  const vencidasRows = rows.filter((r) => r.status_id === STATUS_ID_VENCIDO);
  return {
    totalCount: rows.length, totalValue: sum(rows),
    pagoCount: pagoRows.length, pagoValue: sum(pagoRows),
    aVencerCount: aVencerRows.length, aVencerValue: sum(aVencerRows),
    vencendoCount: vencendoRows.length, vencendoValue: sum(vencendoRows),
    vencidasCount: vencidasRows.length, vencidasValue: sum(vencidasRows),
  };
}

// `scope` = 'month' (mês selecionado, padrão) ou 'all' (todas as contas, sem filtro
// de data nos painéis). O gráfico de movimentações sempre reflete o `year`.
// `filter` = KPI clicado no topo: os cards mantêm os totais completos, mas TODOS
// os gráficos passam a refletir só o subconjunto do KPI (limpar = 'total').
// `skCompany` (opcional): empresa pagadora (1=OTIMOTEX TECIDOS, 2=LEBIANCO, 3=OTIMOTEX FARDOS, 4=LE BLANC); undefined = TODAS.
// Diferente de /consulta (cujos KPIs gerais são globais), aqui o filtro vale para TUDO —
// KPIs, donuts e o gráfico anual —, pois no dashboard todo indicador deriva do escopo.
export async function getDashboardData(month: number, year: number, scope: DashboardScope = 'month', filter: KpiFilter = 'total', skCompany?: number): Promise<DashboardData> {
  // Filtro de empresa pela FK — aplicado nas DUAS leituras (escopo + ano), senão o
  // gráfico de movimentações mensais mostraria as duas empresas.
  const companyFilter = skCompany ? { sk_company: `eq.${skCompany}` } : {};
  const { first, last, todayStr, in7 } = dashboardWindow(month, year);

  // Contas do escopo (exclui cancelado) com embed do fornecedor.
  // 'month' → filtra pelo intervalo do mês; 'all' → todas as contas.
  // As duas leituras são independentes → Promise.all (antes eram sequenciais).
  const [monthRows, yearRows] = await Promise.all([
    query<MonthRow[]>('financial_account_control', {
      select: 'id,amount,status_id,due_date,document_type,payment_method,description,supplier(trade_name,legal_name)',
      status_id: `neq.${STATUS_ID_CANCELADO}`,
      ...companyFilter,
      ...(scope === 'month' ? { and: `(due_date.gte.${first},due_date.lte.${last})` } : {}),
      limit: readLimit(scope),
    }),
    // Contas do ano inteiro (só os campos do gráfico) para as movimentações mês a mês.
    query<YearRow[]>('financial_account_control', {
      select: 'amount,status_id,due_date',
      status_id: `neq.${STATUS_ID_CANCELADO}`,
      ...companyFilter,
      and: `(due_date.gte.${year}-01-01,due_date.lte.${year}-12-31)`,
      limit: ALL_READ_LIMIT,
    }),
  ]);
  warnIfTruncated(monthRows, readLimit(scope), 'contas do escopo');
  warnIfTruncated(yearRows, ALL_READ_LIMIT, 'contas do ano');

  // KPIs
  const kpis = computeKpis(monthRows, todayStr, in7);

  // Aplica o filtro do KPI clicado APENAS aos gráficos (os KPIs acima usam o
  // conjunto completo). 'total' => sem filtro (evita recriar os arrays à toa).
  const fMonth = filter === 'total' ? monthRows : monthRows.filter((r) => matchesKpiFilter(r, filter, todayStr, in7));
  const fYear = filter === 'total' ? yearRows : yearRows.filter((r) => matchesKpiFilter(r, filter, todayStr, in7));

  // Situação por status
  const statusMap = new Map<string, { count: number; value: number }>();
  for (const r of fMonth) {
    const k = STATUS_NAME_BY_ID[r.status_id] ?? 'pendente';
    const cur = statusMap.get(k) ?? { count: 0, value: 0 };
    cur.count += 1; cur.value += num(r.amount);
    statusMap.set(k, cur);
  }
  const statusBreakdown: StatusSlice[] = [...statusMap.entries()]
    .map(([status, v]) => ({ status, ...v }))
    .sort((a, b) => b.count - a.count);

  // Tipos de conta (document_type) e formas de pagamento (payment_method) — Top 8 + "outros".
  // No de tipos de conta, todas as guias tributárias colapsam num único "Tributos".
  const documentTypeBreakdown = breakdownBy(fMonth, (r) => groupDocumentTypeLabel(r.document_type));
  const paymentMethodBreakdown = breakdownBy(fMonth, (r) => r.payment_method);
  // Donut "Tributos": cópia do de tipos de conta, mas só das guias tributárias,
  // detalhadas por tipo (darf, das, gnre, …).
  const taxTypeBreakdown = breakdownBy(fMonth.filter((r) => isTaxDocumentType(r.document_type)), (r) => r.document_type);

  // Ranking de fornecedores (top 6 por valor)
  const supMap = new Map<string, { value: number; count: number }>();
  for (const r of fMonth) {
    const k = supplierName(r);
    const cur = supMap.get(k) ?? { value: 0, count: 0 };
    cur.value += num(r.amount); cur.count += 1;
    supMap.set(k, cur);
  }
  const supplierRanking: SupplierRank[] = [...supMap.entries()]
    // key = identidade do balde deste ranking (agrega por NOME do fornecedor); mantém o
    // contrato SupplierRank compartilhado com o dashboard financeiro. Este ranking não tem
    // drill-down, mas a key deve existir e ser única por balde.
    .map(([name, v]) => ({ key: `sup:${name}`, name, ...v }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 6);

  // Movimentações mês a mês (12 baldes)
  const buckets: MonthlyFlow[] = Array.from({ length: 12 }, (_, m) => ({ month: m, aPagar: 0, pago: 0 }));
  for (const r of fYear) {
    if (!r.due_date) continue;
    const m = Number(r.due_date.slice(5, 7)) - 1;
    if (m < 0 || m > 11) continue;
    buckets[m].aPagar += num(r.amount);
    if (r.status_id === STATUS_ID_PAGO) buckets[m].pago += num(r.amount);
  }

  // Contas críticas / prioritárias: utilidades essenciais OU vencidas.
  const priorityAccounts: PriorityAccount[] = fMonth
    .map((r): PriorityAccount | null => {
      const kind = classifyPriority(`${supplierName(r)} ${r.description ?? ''} ${r.document_type ?? ''}`);
      const isVencido = r.status_id === STATUS_ID_VENCIDO;
      if (!kind && !isVencido) return null;
      return {
        id: r.id, kind: kind ?? 'outro', supplier: supplierName(r),
        due: r.due_date, amount: r.amount,
        // PriorityAccount.status carrega o NOME (o Dashboard renderiza StatusBadge por nome).
        status: STATUS_NAME_BY_ID[r.status_id] ?? 'pendente',
        critical: isVencido,
      };
    })
    .filter((x): x is PriorityAccount => x !== null)
    .sort((a, b) => {
      if (a.critical !== b.critical) return a.critical ? -1 : 1; // vencidas primeiro
      return (a.due ?? '').localeCompare(b.due ?? '');
    })
    .slice(0, 7);

  return { month, year, scope, kpis, statusBreakdown, documentTypeBreakdown, taxTypeBreakdown, paymentMethodBreakdown, supplierRanking, monthlyFlow: buckets, priorityAccounts };
}

// ── dashboard financeiro (DESPESAS + CUSTO) ──────────────────────────────────
// Variante do dashboard escopada a DESPESAS + CUSTO (conta cujo plano de contas tem grupo
// com Natureza "Despesas" OU "Custo", i.e. group.type_group_id ∈ {TYPE_GROUP_ID_DESPESAS,
// TYPE_GROUP_ID_CUSTO} — migration 094; decisão do usuário 2026-07-22). Mantém KPIs e
// filtros (empresa/mês/escopo/KPI), mas NÃO tem o gráfico mês a mês (por isso lê só o mês,
// não o ano); traz 5 donuts — Classificação Financeira (Tipo do subgrupo: Fixa/Variável/
// Custos de Mercadorias/Importação) + os 4 por GRUPO recortados pelo Tipo (7/9/5/6) — e DOIS
// rankings
// por VALOR (R$) — CENTRO DE CUSTO e SUBGRUPO — no lugar do ranking de fornecedores e das
// "Contas críticas e prioritárias" (que seguem só no dashboard de vencimentos). Reusa os
// helpers de getDashboardData (num, breakdownBy, matchesKpiFilter). `month` é 0-indexed.

// Embed aninhado (3 níveis) da classificação contábil — espelha os aliases/FKs já
// validados em SELECT_WITH_EMBEDS, acrescentando type_group_id + a folha type_group para
// (a) identificar o ESCOPO pelo grupo (Natureza 2/8) e (b) rotular o Tipo do subgrupo.
type ExpenseChartAccount = {
  account_code: string | null;
  account_description: string | null;
  group?: { group_description: string | null; type_group_id: number } | null;
  // Do subgrupo interessam: (a) a folha type_group — `type_group_description` rotula o donut
  // "Classificação Financeira" e `type_group_id` (5=Fixa / 6=Variável / 7=Custos de
  // Mercadorias) SEPARA os 3 donuts por grupo (corte pelo ID, nunca pelo texto); (b) a
  // identidade/rótulo do próprio subgrupo (`chart_account_subgroup_id`/`subgroup_code`/
  // `subgroup_description`), base do "Ranking de contas".
  subgroup?: {
    chart_account_subgroup_id: number;
    subgroup_code: string | null;
    subgroup_description: string | null;
    type_group?: { type_group_id: number; type_group_description: string | null } | null;
  } | null;
} | null;

// Linha do mês no dashboard financeiro: só o que os KPIs/gráficos daqui consomem
// (valor, situação, vencimento + classificação). Não herda MonthRow — os campos de
// fornecedor/descrição só serviam às "Contas prioritárias", removidas desta tela.
// `cost_center_id` e o embed `cost_center` saíram em 2026-08-15 junto com o card "Ranking
// de centros de custo": eram a ÚNICA coisa que os lia nesta tela (nem o escopo, nem os
// KPIs, nem os donuts, nem o ranking de contas, nem as colunas do card de detalhe os usam).
type ExpenseMonthRow = Pick<
  FinancialAccountControl,
  'id' | 'amount' | 'status_id' | 'due_date'
> & {
  // `supplier` alimenta a coluna Fornecedor do card de detalhe (drill-down). Embed simples
  // (não `!inner`): `sk_supplier` é NOT NULL, então o objeto vem sempre.
  supplier?: { trade_name: string | null; legal_name: string | null } | null;
  chart_account?: ExpenseChartAccount;
};

// Linha do card de detalhe (drill-down) dos gráficos de /dashboard_despesas: é a MESMA linha
// já lida para os gráficos (fMonth), com `id` + `supplier`. Nenhuma leitura extra por clique.
export type ExpenseDetailRow = ExpenseMonthRow;

// Uma entrada de ranking ANTES da agregação: `key` é a identidade (id da FK), `label` o
// texto exibido e `code` o desambiguador usado quando dois ids têm o mesmo label.
interface RankPick { key: string; label: string; code: string | null }
// Linha sem a dimensão (FK no sentinela 0 / embed ausente) — todas somam num balde só.
const UNRANKED: RankPick = { key: '∅', label: 'não informado', code: null };

/**
 * Monta a entrada de ranking de uma dimensão, ou `null` quando a conta não a tem.
 *
 * O **sentinela id 0** ("não informado") EXISTE nos dois cadastros — com descrição NULL —,
 * então o embed vem PREENCHIDO e testar só `embed != null` não basta: sem o corte por
 * `id > 0` a linha apareceria no ranking como um rótulo técnico (`#0`) em vez de cair no
 * balde "não informado". Descrição vazia recebe o mesmo tratamento.
 */
const rankEntry = (
  prefix: string,
  id: number | null | undefined,
  label: string | null | undefined,
  code: string | null | undefined,
): RankPick | null => {
  const desc = (label ?? '').trim();
  if (!id || id <= 0 || !desc) return null;
  return { key: `${prefix}:${id}`, label: desc, code: code ?? null };
};

export interface FinancialDashboardData {
  month: number; year: number; scope: DashboardScope;
  kpis: DashboardKpis;
  // Por GRUPO (group_description), recortado pelo Tipo do SUBGRUPO: um donut só com as
  // despesas FIXAS (type_group 5), um com as VARIÁVEIS (6) e um com os CUSTOS DE
  // MERCADORIAS (7). Conta cujo subgrupo não está classificado fica fora dos três.
  despesaFixaBreakdown: LabelSlice[];
  despesaVariavelBreakdown: LabelSlice[];
  custoMercadoriasBreakdown: LabelSlice[];
  custoImportacaoBreakdown: LabelSlice[];
  tipoBreakdown: LabelSlice[]; // Fixa/Variável/Custos de Mercadorias/Importação (type_group do subgrupo)
  subgroupRanking: SupplierRank[]; // top SUBGRUPOS de plano de contas por VALOR (card "Ranking de contas")
  // Linhas que alimentam os 5 gráficos (= fMonth, já recortado por escopo/empresa/KPI). O
  // card de detalhe (drill-down) filtra ESTE array em memória via filterExpenseDetailRows —
  // sem leitura extra e sempre um subconjunto EXATO do que a fatia/linha contou.
  detailRows: ExpenseDetailRow[];
}

// Qual gráfico foi clicado. Donuts identificam o balde pelo `label`; o ranking pela `bucketKey`.
// 'grupoTipo' = os donuts POR GRUPO recortados pelo Tipo do subgrupo (Despesas Fixas /
// Variáveis / Custos de Mercadorias / Importação) — genérico via `typeGroupId`, em vez de um
// case por donut. ('costCenter' saiu em 2026-08-15 com o card de centros de custo.)
type ExpenseDrillChart = 'tipo' | 'grupoTipo' | 'subgroup';
export interface ExpenseDrillTarget {
  chart: ExpenseDrillChart;
  label?: string;       // donuts: rótulo da fatia clicada (pode ser 'outros' / 'não informado')
  bucketKey?: string;   // rankings: RankPick.key da linha clicada (SupplierRank.key)
  typeGroupId?: number; // 'grupoTipo': o Tipo do subgrupo que recorta o donut (5/6/7/9)
}

// Casa as linhas de UM balde de donut (reproduz breakdownBy): fatia própria → rótulo igual;
// fatia sintética "outros" → tudo que NÃO está no top-N. `rows` já vem pré-filtrado (ex.: só
// type_group 5 para o donut "Despesas Fixas"), então o top-N é calculado sobre esse recorte.
function matchDonutBucket(
  rows: ExpenseDetailRow[], pick: (r: ExpenseDetailRow) => string | null, label: string,
): ExpenseDetailRow[] {
  const top = topBucketLabels(rows, pick);
  const norm = (r: ExpenseDetailRow): string => pick(r) ?? 'não informado';
  return top.has(label) ? rows.filter((r) => norm(r) === label) : rows.filter((r) => !top.has(norm(r)));
}

const tipoOf = (r: ExpenseDetailRow): number | null | undefined =>
  r.chart_account?.subgroup?.type_group?.type_group_id;
const tipoDescOf = (r: ExpenseDetailRow): string | null =>
  r.chart_account?.subgroup?.type_group?.type_group_description ?? null;
const grupoOf = (r: ExpenseDetailRow): string | null => r.chart_account?.group?.group_description ?? null;
const sgKeyOf = (r: ExpenseDetailRow): string =>
  rankEntry('sg', r.chart_account?.subgroup?.chart_account_subgroup_id,
    r.chart_account?.subgroup?.subgroup_description, r.chart_account?.subgroup?.subgroup_code)?.key
  ?? UNRANKED.key;

/**
 * Contas que compõem a fatia/linha clicada — MESMO recorte usado na agregação de cada
 * gráfico (por isso reproduz a contagem/valor exibidos). Puro e testável.
 */
export function filterExpenseDetailRows(
  rows: ExpenseDetailRow[], target: ExpenseDrillTarget,
): ExpenseDetailRow[] {
  const { chart, label, bucketKey, typeGroupId } = target;
  switch (chart) {
    case 'tipo':
      return matchDonutBucket(rows, tipoDescOf, label ?? '');
    case 'grupoTipo':
      // Donut por GRUPO recortado pelo Tipo do subgrupo informado (5/6/7/9 — o 9, Custos de
      // Importação, entrou em 2026-08-14 e faltava nesta lista) — o MESMO
      // pré-filtro da partição que gera os breakdowns, então reproduz a fatia exata.
      // Alvo sem typeGroupId é malformado → nada casa. A guarda é REAL (não só o teste):
      // sem ela, `tipoOf(r) === undefined` casaria linha SEM embed de subgrupo
      // (undefined === undefined) e o ramo "outros" devolveria as não-classificadas.
      if (typeGroupId == null) return [];
      return matchDonutBucket(rows.filter((r) => tipoOf(r) === typeGroupId), grupoOf, label ?? '');
    case 'subgroup':
      return rows.filter((r) => sgKeyOf(r) === bucketKey);
    default:
      return [];
  }
}

// Escopo do dashboard = grupo do plano de contas com Natureza "Despesas" OU "Custo"
// (type_group_id 2 ou 8 — decisão do usuário 2026-07-22: custo de mercadoria é conta a
// pagar e entra em TODA métrica). Conta sem classificação (chart_account nulo / grupo
// ausente / outra natureza, ex. Passivo) fica FORA de tudo.
const isExpenseRow = (r: { chart_account?: { group?: { type_group_id: number } | null } | null }): boolean => {
  const tg = r.chart_account?.group?.type_group_id;
  return tg === TYPE_GROUP_ID_DESPESAS || tg === TYPE_GROUP_ID_CUSTO;
};

// Linhas exibidas no ranking do dashboard financeiro (subgrupo do plano de contas).
const RANKING_TOP_N = 12;

export async function getFinancialDashboardData(month: number, year: number, scope: DashboardScope = 'month', filter: KpiFilter = 'total', skCompany?: number): Promise<FinancialDashboardData> {
  const companyFilter = skCompany ? { sk_company: `eq.${skCompany}` } : {};
  const { first, last, todayStr, in7 } = dashboardWindow(month, year);

  // Leitura ÚNICA (a do ANO saiu junto com o gráfico mês a mês): embed aninhado da
  // classificação (grupo/subgrupo + type_group).
  const monthRowsAll = await query<ExpenseMonthRow[]>('financial_account_control', {
    select:
      'id,amount,status_id,due_date,' +
      'supplier(trade_name,legal_name),' +
      'chart_account:financial_chart_of_account(account_code,account_description,' +
      'group:financial_chart_of_account_group(group_description,type_group_id),' +
      'subgroup:financial_chart_of_account_subgroup(chart_account_subgroup_id,subgroup_code,subgroup_description,' +
      'type_group:financial_type_group(type_group_id,type_group_description)))',
    status_id: `neq.${STATUS_ID_CANCELADO}`,
    ...companyFilter,
    ...(scope === 'month' ? { and: `(due_date.gte.${first},due_date.lte.${last})` } : {}),
    limit: readLimit(scope),
  });

  warnIfTruncated(monthRowsAll, readLimit(scope), 'despesas do escopo');

  // Escopo DESPESAS aplicado antes de qualquer agregação.
  const monthRows = monthRowsAll.filter(isExpenseRow);

  // KPIs (sobre as despesas do mês — conjunto completo, sem o filtro de KPI clicado).
  const kpis = computeKpis(monthRows, todayStr, in7);

  // Filtro do KPI clicado: só afeta os gráficos (os cards mantêm os totais).
  const fMonth = filter === 'total' ? monthRows : monthRows.filter((r) => matchesKpiFilter(r, filter, todayStr, in7));

  // Donuts "Despesas Fixas", "Despesas Variáveis", "Custos de Mercadorias" e "Custos de
  // Importação": mesma dimensão (GRUPO, group_description), recortada pelo Tipo do SUBGRUPO. O
  // corte é pelo `type_group_id` (5/6/7/9, constantes do catálogo) e NÃO pela descrição — o
  // texto é livre e renomear a linha do catálogo esvaziaria os donuts em silêncio.
  // 🔴 O 4º donut foi acrescentado em 2026-08-14 (achado da migration 127/128): o tipo 9
  // (Custos de Importação) existia no catálogo e contava certo no TOTAL do dashboard
  // (isExpenseRow reconhece a NATUREZA do grupo, 2/8), mas não entrava em NENHUM dos 3 donuts
  // de tipo — mesma classe de bug do `demonstrativo_despesas` (CASE hardcoded que não sabia do
  // tipo novo), só que aqui do lado do TypeScript.
  // Partição numa passada só; conta com subgrupo não classificado (id 0 / embed ausente)
  // não entra em nenhum dos quatro — não há balde residual, por decisão de produto.
  const fixaRows: ExpenseMonthRow[] = [];
  const variavelRows: ExpenseMonthRow[] = [];
  const custoMercRows: ExpenseMonthRow[] = [];
  const custoImpRows: ExpenseMonthRow[] = [];
  for (const r of fMonth) {
    const tipo = r.chart_account?.subgroup?.type_group?.type_group_id;
    if (tipo === TYPE_GROUP_ID_DESPESA_FIXA) fixaRows.push(r);
    else if (tipo === TYPE_GROUP_ID_DESPESA_VARIAVEL) variavelRows.push(r);
    else if (tipo === TYPE_GROUP_ID_CUSTO_MERCADORIAS) custoMercRows.push(r);
    else if (tipo === TYPE_GROUP_ID_CUSTO_IMPORTACAO) custoImpRows.push(r);
  }
  const porGrupo = (rows: ExpenseMonthRow[]): LabelSlice[] =>
    breakdownBy(rows, (r) => r.chart_account?.group?.group_description ?? null);
  const despesaFixaBreakdown = porGrupo(fixaRows);
  const despesaVariavelBreakdown = porGrupo(variavelRows);
  const custoMercadoriasBreakdown = porGrupo(custoMercRows);
  const custoImportacaoBreakdown = porGrupo(custoImpRows);
  // Donut "Classificação Financeira": Fixa/Variável/Custos de Mercadorias/Importação (descrição
  // do type_group do SUBGRUPO — vem do catálogo, sem literal; migrations 092/093).
  const tipoBreakdown = breakdownBy(fMonth, (r) => r.chart_account?.subgroup?.type_group?.type_group_description ?? null);

  // Ranking por VALOR (R$). Top 12 (o espaço liberado pelo gráfico mês a mês passou a caber
  // mais linhas). Continua um helper genérico, e não inline no único chamador: `pick` é o
  // ponto de extensão para uma dimensão nova, e é o que mantém a regra de agregação abaixo
  // em UM lugar só.
  //
  // Agrega pela IDENTIDADE do cadastro (o id da FK), NUNCA pelo texto:
  // `financial_chart_of_account_subgroup` não tem UNIQUE em descrição (só a PK; o CRUD
  // valida o CÓDIGO, e só na aplicação). Agregando por texto, dois cadastros homônimos
  // virariam UMA linha somada — dado errado e silencioso — e ainda colidiriam na `key` do
  // RankingList. O texto entra só como RÓTULO.
  const rankBy = (pick: (r: ExpenseMonthRow) => RankPick | null): SupplierRank[] => {
    const map = new Map<string, { value: number; count: number; label: string; code: string | null }>();
    for (const r of fMonth) {
      const p = pick(r) ?? UNRANKED;
      const cur = map.get(p.key) ?? { value: 0, count: 0, label: p.label, code: p.code };
      cur.value += num(r.amount); cur.count += 1;
      map.set(p.key, cur);
    }
    // entries() preserva a `key` do balde (identidade) além de value/count/label/code — o
    // drill-down casa por essa key, então ela precisa sair no objeto de ranking.
    const rows = [...map.entries()].map(([key, v]) => ({ key, ...v }));
    // Rótulos iguais vindos de ids DIFERENTES: prefixa o código para o usuário distinguir
    // as duas linhas (e para a `key` do RankingList continuar única).
    const repetidos = new Set(rows.map((r) => r.label).filter((l, i, all) => all.indexOf(l) !== i));
    // % do TOTAL de registros do escopo (fMonth, ANTES do corte top-N) — não da soma dos
    // valores exibidos. Cada `count` de bucket soma exatamente a `total` (todo registro de
    // fMonth cai em algum bucket, inclusive o sentinela UNRANKED), então as % de TODOS os
    // buckets somam 100% mesmo que o top-N exibido não some.
    const total = fMonth.length;
    return rows
      .map((r) => ({
        key: r.key,
        name: repetidos.has(r.label) && r.code ? `${r.code} — ${r.label}` : r.label,
        value: r.value,
        count: r.count,
        pct: total ? (r.count / total) * 100 : 0,
      }))
      .sort((a, b) => b.value - a.value)
      .slice(0, RANKING_TOP_N);
  };

  // Ranking por SUBGRUPO do plano de contas (o card mantém o rótulo "Ranking de contas"):
  // agrega pela IDENTIDADE do subgrupo (`chart_account_subgroup_id`) — nunca pelo texto, que
  // não é UNIQUE no cadastro —, com rótulo = descrição do subgrupo (código prefixado só quando
  // dois subgrupos forem homônimos, via rankBy). Contas cujo plano não tem subgrupo (sentinela
  // id 0 / embed ausente) caem no balde "não informado".
  const subgroupRanking = rankBy((r) =>
    rankEntry(
      'sg',
      r.chart_account?.subgroup?.chart_account_subgroup_id,
      r.chart_account?.subgroup?.subgroup_description,
      r.chart_account?.subgroup?.subgroup_code,
    ),
  );

  return { month, year, scope, kpis, despesaFixaBreakdown, despesaVariavelBreakdown, custoMercadoriasBreakdown, custoImportacaoBreakdown, tipoBreakdown, subgroupRanking, detailRows: fMonth };
}
