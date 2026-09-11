// src/services/lookups.ts
// Cliente dos lookups de classificação contábil (centro de custo / plano de
// contas) na Next API (apps/api-backend, :3000). Mesmo motivo do CRUD de
// fornecedores: as tabelas têm RLS, a leitura via service_role vive no backend.
// O token da sessão do Supabase vai no Authorization; o middleware o valida.

import type { CostCenter, Bank, ChartAccountGroup, ChartAccountSubgroup } from '@sheild/shared';
import { supabase } from '../lib/supabaseClient';

// Opções da cascata INVERTIDA de classificação contábil (Plano → Centro). Espelham os
// tipos do backend (lib/lookups.ts). Sem schema compartilhado — o consumidor infere.
// ChartAccountDescriptionOption fica MÓDULO-PRIVADO (só o tipo de retorno de
// listPlanoDescriptions; o ChartAccountSelect infere pela chamada); CenterForPlanoOption é
// exportado porque o CostCenterSelect o nomeia.
interface ChartAccountDescriptionOption {
  account_description: string;
}

export interface CenterForPlanoOption {
  chart_account_id: number;
  account_code: string | null;
  cost_center_id: number;
  cost_center_description: string | null;
}

// Linha da dimensão `status` (lookup de situação do CRUD de contas). Espelha o tipo
// do backend (lib/lookups.ts StatusOption) — não há schema compartilhado p/ status.
export interface StatusOption {
  status_id: number;
  status_name: string | null;
}

// Linha do cadastro `company` (lookup da empresa pagadora no ContaForm). Espelha o tipo do
// backend (lib/lookups.ts CompanyOption) — não há schema compartilhado p/ o lookup de empresa
// (o `companyEmbeddedSchema` do shared é só o embed de leitura, sem o sk). Sem `export`: o
// consumidor (ContaForm) infere a partir de listCompanies() e nunca nomeia o tipo.
interface CompanyOption {
  sk_company: number;
  trade_name: string | null;
}

// Linha do catálogo `financial_type_group` (NATUREZA contábil — lookup do <select>
// "Natureza" no CRUD de Grupos). Espelha o tipo do backend (lib/lookups.ts TypeGroupOption).
// Sem `export` (como CompanyOption): o consumidor (ChartAccountGroupsPage) infere pelo retorno.
interface TypeGroupOption {
  type_group_id: number;
  type_group_description: string | null;
}

const DATA_API_BASE = (import.meta.env.VITE_DATA_API_URL as string | undefined) ?? '/data-api';

interface ApiEnvelope<T> {
  success: boolean;
  data?: T;
  error?: string;
}

async function authHeaders(): Promise<Record<string, string>> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token ?? '';
  return { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` };
}

async function call<T>(path: string): Promise<T> {
  const res = await fetch(`${DATA_API_BASE}${path}`, { headers: await authHeaders() });
  const body = (await res.json().catch(() => ({}))) as ApiEnvelope<T>;
  if (!res.ok || !body.success) {
    throw new Error(body.error ?? `Erro ${res.status} ao acessar a API de dados`);
  }
  return body.data as T;
}

export async function listCostCenters(search?: string): Promise<CostCenter[]> {
  const qs = search ? `?search=${encodeURIComponent(search)}` : '';
  return call<CostCenter[]>(`/cost-centers${qs}`);
}

// Cascata INVERTIDA — 1º select: descrições distintas de planos de contas postáveis
// (com centro válido). Busca textual opcional por código/descrição.
export async function listPlanoDescriptions(search?: string): Promise<ChartAccountDescriptionOption[]> {
  const qs = search ? `?search=${encodeURIComponent(search)}` : '';
  return call<ChartAccountDescriptionOption[]>(`/chart-accounts${qs}`);
}

// Cascata INVERTIDA — 2º select: os centros de custo que compõem o plano (descrição)
// escolhido. Sem descrição (`null`), retorna [] sem ir à rede (o centro depende do plano).
export async function listCentersForPlano(description: string | null): Promise<CenterForPlanoOption[]> {
  if (!description) return [];
  const qs = new URLSearchParams({ description });
  return call<CenterForPlanoOption[]>(`/chart-accounts?${qs.toString()}`);
}

// ── Lookups dos cadastros do grupo Tabelas (modo lookup = rota sem `page`) ────────
// Listas completas (cadastros pequenos) p/ os <select> dos formulários do CRUD.

export function listBanks(search?: string): Promise<Bank[]> {
  const qs = search ? `?search=${encodeURIComponent(search)}` : '';
  return call<Bank[]>(`/banks${qs}`);
}

export function listChartAccountGroups(search?: string): Promise<ChartAccountGroup[]> {
  const qs = search ? `?search=${encodeURIComponent(search)}` : '';
  return call<ChartAccountGroup[]>(`/chart-account-groups${qs}`);
}

export function listChartAccountSubgroups(search?: string): Promise<ChartAccountSubgroup[]> {
  const qs = search ? `?search=${encodeURIComponent(search)}` : '';
  return call<ChartAccountSubgroup[]>(`/chart-account-subgroups${qs}`);
}

export function listStatuses(): Promise<StatusOption[]> {
  return call<StatusOption[]>('/statuses');
}

// Empresas pagadoras (cadastro `company`, hoje 4 linhas) — lookup do <select> "Empresa" do ContaForm.
export function listCompanies(): Promise<CompanyOption[]> {
  return call<CompanyOption[]>('/companies');
}

// Catálogo `financial_type_group` ESCOPADO (migration 094) — alimenta o <select>
// "Natureza" do CRUD de Grupos (scope 'group': Receitas/Despesas/Ativo/Passivo) e "Tipo"
// do CRUD de Sub grupos (scope 'subgroup': Despesas Fixas/Variáveis). Ambos incluem o id 0
// ("Não informado", escopo 'both'), permitindo desclassificar. Sem `scope`, retorna todas.
export function listFinancialTypeGroups(scope?: 'group' | 'subgroup'): Promise<TypeGroupOption[]> {
  const qs = scope ? `?scope=${scope}` : '';
  return call<TypeGroupOption[]>(`/financial-type-groups${qs}`);
}
