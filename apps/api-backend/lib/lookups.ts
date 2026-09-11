// lib/lookups.ts
// Lookups de classificação contábil (centro de custo / plano de contas) sobre o
// Supabase (service_role, ignora RLS). São cadastros pré-existentes só-leitura,
// consumidos pelos react-select do formulário de contas. Service simples (sem
// Repository dedicado): a leitura é trivial e não há escrita.

import { type CostCenter } from '@sheild/shared';
import { getSupabaseAdmin } from './supabase-admin';
import { ApiServiceError } from './api-error';

const COST_CENTER_TABLE = 'financial_cost_center';
const CHART_ACCOUNT_TABLE = 'financial_chart_of_account';
const STATUS_TABLE = 'status';
const COMPANY_TABLE = 'company';
const TYPE_GROUP_TABLE = 'financial_type_group';
const DEFAULT_LIMIT = 500; // cadastros pequenos (dezenas/centenas) — cabe num fetch.
const MAX_LIMIT = 1000;

// Linha da dimensão `status` (lookup do CRUD de contas — financial_account.status_id).
export interface StatusOption {
  status_id: number;
  status_name: string | null;
}

// Linha do cadastro `company` (lookup da empresa pagadora no ContaForm —
// financial_account_control.sk_company). Hoje 4 linhas: OTIMOTEX TECIDOS (1), LEBIANCO (2),
// OTIMOTEX FARDOS (3) e LE BLANC (4). Sem filtro/limite — empresa nova aparece sozinha nos selects.
export interface CompanyOption {
  sk_company: number;
  trade_name: string | null;
}

// Linha do catálogo `financial_type_group` — lookup dos <select>s "Natureza" (CRUD de
// Grupos) e "Tipo" (CRUD de Sub grupos). O catálogo hospeda DUAS taxonomias distintas
// (Natureza do grupo: Receitas/Despesas/Ativo/Passivo; Tipo do subgrupo: Despesas
// Fixas/Variáveis), discriminadas por `applies_to` (migration 094). Ordenado por id.
export interface TypeGroupOption {
  type_group_id: number;
  type_group_description: string | null;
}

// Escopo de aplicação de um type_group (discriminador `applies_to`). 'both' (id 0
// "Não informado") aparece nos dois cadastros; os demais são exclusivos de um.
export type TypeGroupScope = 'group' | 'subgroup';

// Opção do 1º select da classificação contábil (cascata INVERTIDA Plano → Centro):
// descrições DISTINTAS de planos postáveis. A mesma descrição ("Serviços Gerais") existe
// como várias linhas (uma por centro), então a lista é deduplicada por descrição.
export interface ChartAccountDescriptionOption {
  account_description: string;
}

// Opção do 2º select (centro de custo QUE COMPÕE o plano escolhido): cada linha do plano
// com aquela descrição resolve um `chart_account_id` específico e o seu `cost_center_id`.
// `account_code` distingue os casos raros de mesma descrição repetida no mesmo centro.
export interface CenterForPlanoOption {
  chart_account_id: number;
  account_code: string | null;
  cost_center_id: number;
  cost_center_description: string | null;
}

export class LookupServiceError extends ApiServiceError {
  constructor(message: string, status: number) {
    super(message, status, 'LookupServiceError');
  }
}

interface LookupListParams {
  search?: string;
  limit?: number;
}

// Sanitiza o termo para o filtro `or` do PostgREST (chars de controle da sintaxe).
function sanitizeTerm(term: string): string {
  return term.replace(/[%,()]/g, ' ').trim();
}

function clampLimit(limit?: number): number {
  return Math.min(MAX_LIMIT, Math.max(1, Math.trunc(limit ?? DEFAULT_LIMIT)));
}

export const costCenterService = {
  /**
   * Lista centros de custo (apenas os com descrição — descarta o placeholder
   * sem código/descrição). Busca textual opcional por código/descrição.
   * @throws {LookupServiceError} 500 em falha do banco.
   */
  async list(params: LookupListParams = {}): Promise<CostCenter[]> {
    let query = getSupabaseAdmin()
      .from(COST_CENTER_TABLE)
      .select('cost_center_id,cost_center_code,cost_center_description')
      .not('cost_center_description', 'is', null);

    const term = params.search?.trim() ? sanitizeTerm(params.search) : '';
    if (term) {
      query = query.or(`cost_center_code.ilike.%${term}%,cost_center_description.ilike.%${term}%`);
    }

    const { data, error } = await query.order('cost_center_description', { ascending: true }).limit(clampLimit(params.limit));
    if (error) throw new LookupServiceError(error.message, 500);
    return (data ?? []) as CostCenter[];
  },
};

// Linha crua de financial_chart_of_account com o embed do centro (to-one).
interface ChartAccountRow {
  chart_account_id: number;
  account_code: string | null;
  cost_center_id: number;
  cost_center: { cost_center_code: string | null; cost_center_description: string | null } | null;
}

export const chartAccountService = {
  /**
   * 1º select da classificação (cascata INVERTIDA Plano → Centro): lista as DESCRIÇÕES
   * distintas de planos postáveis com centro válido (cost_center_id > 0). A mesma descrição
   * existe em várias linhas (uma por centro), então deduplica por descrição preservando a
   * ordem alfabética. Busca textual opcional por código/descrição.
   * @throws {LookupServiceError} 500 em falha do banco.
   */
  async listPlanoDescriptions(params: LookupListParams = {}): Promise<ChartAccountDescriptionOption[]> {
    let query = getSupabaseAdmin()
      .from(CHART_ACCOUNT_TABLE)
      .select('account_description')
      .is('is_postable', true)
      .not('account_description', 'is', null)
      .gt('cost_center_id', 0);

    const term = params.search?.trim() ? sanitizeTerm(params.search) : '';
    if (term) {
      query = query.or(`account_code.ilike.%${term}%,account_description.ilike.%${term}%`);
    }

    // A dedup é em JS, então o LIMIT precisa cobrir TODAS as linhas postáveis (uma descrição
    // se repete por centro): truncar antes de deduplicar esconderia descrições no fim do
    // alfabeto. Usa MAX_LIMIT por padrão (catálogo pequeno, ~centenas < 1000). Se o cadastro
    // ultrapassar MAX_LIMIT linhas postáveis, migrar para DISTINCT via RPC/paginação.
    const { data, error } = await query
      .order('account_description', { ascending: true })
      .limit(clampLimit(params.limit ?? MAX_LIMIT));
    if (error) throw new LookupServiceError(error.message, 500);

    // Deduplica por descrição (o PostgREST não faz DISTINCT) preservando a ordem já ordenada.
    const seen = new Set<string>();
    const out: ChartAccountDescriptionOption[] = [];
    for (const row of (data ?? []) as { account_description: string | null }[]) {
      const desc = row.account_description;
      if (desc && !seen.has(desc)) {
        seen.add(desc);
        out.push({ account_description: desc });
      }
    }
    return out;
  },

  /**
   * 2º select da classificação: os CENTROS DE CUSTO que compõem o plano escolhido — todas
   * as linhas postáveis com a descrição informada, cada uma resolvendo um `chart_account_id`
   * específico + o seu `cost_center_id`. Sem descrição, retorna [] sem consultar o banco
   * (o centro depende do plano; evita carga).
   * @throws {LookupServiceError} 500 em falha do banco.
   */
  async listCentersForPlano(params: { description?: string; limit?: number } = {}): Promise<CenterForPlanoOption[]> {
    const description = params.description?.trim();
    if (!description) return [];

    const { data, error } = await getSupabaseAdmin()
      .from(CHART_ACCOUNT_TABLE)
      .select('chart_account_id,account_code,cost_center_id,cost_center:financial_cost_center(cost_center_code,cost_center_description)')
      .is('is_postable', true)
      .eq('account_description', description)
      .gt('cost_center_id', 0)
      .order('cost_center_id', { ascending: true })
      .limit(clampLimit(params.limit));
    if (error) throw new LookupServiceError(error.message, 500);

    return ((data ?? []) as unknown as ChartAccountRow[]).map((r) => ({
      chart_account_id: r.chart_account_id,
      account_code: r.account_code,
      cost_center_id: r.cost_center_id,
      cost_center_description: r.cost_center?.cost_center_description ?? r.cost_center?.cost_center_code ?? null,
    }));
  },
};

export const statusService = {
  /**
   * Lista a dimensão `status` (lookup de situação do CRUD de contas — alimenta o
   * <select> de `financial_account.status_id`). Ordenada por id.
   * @throws {LookupServiceError} 500 em falha do banco.
   */
  async list(): Promise<StatusOption[]> {
    const { data, error } = await getSupabaseAdmin()
      .from(STATUS_TABLE)
      .select('status_id,status_name')
      .order('status_id', { ascending: true });
    if (error) throw new LookupServiceError(error.message, 500);
    return (data ?? []) as StatusOption[];
  },
};

export const companyService = {
  /**
   * Lista o cadastro `company` (lookup da EMPRESA PAGADORA — alimenta o <select> de
   * `financial_account_control.sk_company` no ContaForm). Cadastro minúsculo (4 linhas:
   * OTIMOTEX TECIDOS/LEBIANCO/OTIMOTEX FARDOS/LE BLANC) e só-leitura — sem busca nem paginação, como o statusService.
   * Ordenado por nome para a lista ficar estável na tela.
   * @throws {LookupServiceError} 500 em falha do banco.
   */
  async list(): Promise<CompanyOption[]> {
    const { data, error } = await getSupabaseAdmin()
      .from(COMPANY_TABLE)
      .select('sk_company,trade_name')
      .order('trade_name', { ascending: true });
    if (error) throw new LookupServiceError(error.message, 500);
    return (data ?? []) as CompanyOption[];
  },
};

export const financialTypeGroupService = {
  /**
   * Lista o catálogo `financial_type_group`, ESCOPADO por `applies_to` (migration 094).
   * Alimenta o <select> "Natureza" (Grupos, scope 'group') e "Tipo" (Sub grupos, scope
   * 'subgroup'). Com escopo, retorna as linhas do próprio escopo + as 'both' (o id 0
   * "Não informado", que permite desclassificar); sem escopo, retorna TODAS (retrocompat).
   * Escopar evita oferecer opções sem sentido (subgrupo com Natureza de grupo e vice-versa).
   * @throws {LookupServiceError} 500 em falha do banco.
   */
  async list(params: { scope?: TypeGroupScope } = {}): Promise<TypeGroupOption[]> {
    let query = getSupabaseAdmin()
      .from(TYPE_GROUP_TABLE)
      .select('type_group_id,type_group_description')
      .order('type_group_id', { ascending: true });
    if (params.scope) query = query.in('applies_to', ['both', params.scope]);
    const { data, error } = await query;
    if (error) throw new LookupServiceError(error.message, 500);
    return (data ?? []) as TypeGroupOption[];
  },
};

/**
 * Valida que o `type_group_id` referenciado é compatível com o cadastro (Finding 2 da
 * revisão): impede atribuir a um grupo uma Natureza de subgrupo (Fixas/Variáveis) e
 * vice-versa — a FK só garante EXISTÊNCIA, não validade semântica. Autoritativa em
 * aplicação (única via de escrita destes cadastros é a Next API/service_role; não há
 * pipeline gravando aqui), no mesmo espírito de lib/classification.ts.
 *
 * Retorna uma mensagem de erro (para 422) ou `null` quando válido. O id 0 ("Não
 * informado", applies_to 'both') é sempre válido e pula a consulta ao banco.
 * @throws {LookupServiceError} 500 em falha do banco.
 */
export async function validateTypeGroupScope(typeGroupId: number, scope: TypeGroupScope): Promise<string | null> {
  if (typeGroupId === 0) return null;
  const { data, error } = await getSupabaseAdmin()
    .from(TYPE_GROUP_TABLE)
    .select('applies_to')
    .eq('type_group_id', typeGroupId)
    .maybeSingle();
  if (error) throw new LookupServiceError(error.message, 500);
  if (!data) return scope === 'group' ? 'Natureza informada não existe' : 'Tipo informado não existe';
  const applies = (data as { applies_to: string }).applies_to;
  if (applies !== 'both' && applies !== scope) {
    return scope === 'group'
      ? 'Natureza inválida para grupo (use Receitas, Despesas, Ativo ou Passivo)'
      : 'Tipo inválido para subgrupo (use Despesas Fixas ou Despesas Variáveis)';
  }
  return null;
}
