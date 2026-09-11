import { describe, it, expect, vi } from 'vitest';
import { TOOL_DEFINITIONS, isToolName, parseToolInput, runTool, ANALYTICS_SCHEMA } from './tools';

describe('TOOL_DEFINITIONS', () => {
  // A lista é travada de propósito: acrescentar tool muda a DEFINIÇÃO enviada ao modelo, o que
  // invalida os três níveis de prompt cache (tools + system + messages). Tem de ser deliberado.
  it('expõe exatamente as 12 tools (098 · 104 · 106 · 108 · 118 · pontualidade da 121 — Onda 9)', () => {
    expect(TOOL_DEFINITIONS.map((t) => t.name)).toEqual([
      'resumo_situacao',
      'gasto_por_periodo',
      'gasto_por_fornecedor',
      'gasto_por_classificacao',
      'demonstrativo_despesas',
      'aging_vencidos',
      'listar_contas',
      'buscar_emails',
      'documentos_fiscais',
      'auditoria_eventos',
      'auditoria_resumo',
      'pontualidade_pagamento',
    ]);
  });

  it('toda tool tem input_schema de objeto — exigência da Claude API', () => {
    for (const t of TOOL_DEFINITIONS) {
      expect(t.input_schema.type, t.name).toBe('object');
      expect(t.description.length, t.name).toBeGreaterThan(30);
    }
  });

  it('isToolName rejeita nome fora do conjunto', () => {
    expect(isToolName('resumo_situacao')).toBe(true);
    expect(isToolName('drop_table')).toBe(false);
  });
});

describe('parseToolInput', () => {
  it('prefixa os parâmetros com p_ para casar a assinatura SQL', () => {
    const r = parseToolInput('gasto_por_periodo', {
      date_from: '2026-07-01',
      date_to: '2026-07-31',
      granularity: 'mes',
    });
    expect(r).toEqual({
      ok: true,
      params: { p_date_from: '2026-07-01', p_date_to: '2026-07-31', p_granularity: 'mes' },
    });
  });

  it('omite chaves ausentes em vez de enviar undefined (deixa o DEFAULT do SQL valer)', () => {
    const r = parseToolInput('resumo_situacao', {});
    expect(r).toEqual({ ok: true, params: {} });
  });

  it('descarta chave desconhecida inventada pelo modelo', () => {
    const r = parseToolInput('resumo_situacao', { sk_company: 2, tabela: 'auth.users' });
    expect(r.ok && r.params).toEqual({ p_sk_company: 2 });
  });

  it('rejeita data em formato errado com mensagem acionável', () => {
    const r = parseToolInput('gasto_por_periodo', { date_from: '01/07/2026', date_to: '2026-07-31' });
    expect(r.ok).toBe(false);
    expect(!r.ok && r.message).toContain('date_from');
    expect(!r.ok && r.message).toContain('YYYY-MM-DD');
  });

  it('rejeita obrigatório ausente', () => {
    const r = parseToolInput('gasto_por_periodo', { date_from: '2026-07-01' });
    expect(r.ok).toBe(false);
    expect(!r.ok && r.message).toContain('date_to');
  });

  it('rejeita group_by fora do domínio (defesa antes do banco)', () => {
    const r = parseToolInput('gasto_por_classificacao', {
      date_from: '2026-07-01',
      date_to: '2026-07-31',
      group_by: 'DROP TABLE',
    });
    expect(r.ok).toBe(false);
  });

  // ---- Onda 1 (migration 104): eixo `tipo`, filtros de compliance e demonstrativo ----

  it('aceita group_by="tipo" (despesa fixa × variável — migration 104)', () => {
    const r = parseToolInput('gasto_por_classificacao', {
      date_from: '2026-07-01',
      date_to: '2026-07-31',
      group_by: 'tipo',
    });
    expect(r.ok).toBe(true);
    expect(r.ok && r.params.p_group_by).toBe('tipo');
  });

  it('prefixa subgroup_type_ids para o parâmetro nomeado da função SQL', () => {
    const r = parseToolInput('gasto_por_classificacao', {
      date_from: '2026-07-01',
      date_to: '2026-07-31',
      group_by: 'subgrupo',
      subgroup_type_ids: [5, 6],
    });
    expect(r.ok && r.params.p_subgroup_type_ids).toEqual([5, 6]);
  });

  // "Boleto sem nota fiscal" é o achado de compliance mais material da base (169 contas). O
  // tri-state importa: `false` PRECISA chegar ao banco, e um teste que só cobrisse `true` deixaria
  // passar um bug em que o falsy fosse descartado junto com o undefined.
  it.each([
    'listar_contas',
    'gasto_por_fornecedor',
  ] as const)('%s repassa has_invoice=false e has_bank_slip=true (tri-state)', (tool) => {
    const r = parseToolInput(tool, {
      date_from: '2026-07-01',
      date_to: '2026-07-31',
      has_invoice: false,
      has_bank_slip: true,
    });
    expect(r.ok).toBe(true);
    expect(r.ok && r.params.p_has_invoice).toBe(false);
    expect(r.ok && r.params.p_has_bank_slip).toBe(true);
  });

  it.each([
    'listar_contas',
    'gasto_por_fornecedor',
  ] as const)('%s omite as flags quando não informadas (não vira filtro implícito)', (tool) => {
    const r = parseToolInput(tool, { date_from: '2026-07-01', date_to: '2026-07-31' });
    expect(r.ok).toBe(true);
    expect(r.ok && 'p_has_invoice' in r.params).toBe(false);
    expect(r.ok && 'p_has_bank_slip' in r.params).toBe(false);
  });

  it('demonstrativo_despesas exige o período e aceita empresa', () => {
    expect(parseToolInput('demonstrativo_despesas', { date_from: '2026-07-01' }).ok).toBe(false);
    const r = parseToolInput('demonstrativo_despesas', {
      date_from: '2026-07-01',
      date_to: '2026-07-31',
      sk_company: 2,
    });
    expect(r.ok && r.params.p_sk_company).toBe(2);
  });

  // ---- Onda 2 (migration 106): busca textual nos e-mails ----

  it('buscar_emails exige o termo e rejeita termo curto demais', () => {
    expect(parseToolInput('buscar_emails', {}).ok).toBe(false);
    expect(parseToolInput('buscar_emails', { termo: 'a' }).ok).toBe(false);
    expect(parseToolInput('buscar_emails', { termo: 'reajuste' }).ok).toBe(true);
  });

  // Os status de e-mail são um domínio DIFERENTE do de contas. Aceitar aqui um nome de status de
  // conta faria o modelo filtrar por valor inexistente na tabela e concluir "não há e-mails".
  it('buscar_emails usa o domínio de status de E-MAIL, não o de contas', () => {
    const base = { termo: 'contrato' };
    expect(parseToolInput('buscar_emails', { ...base, status: ['ignorado'] }).ok).toBe(true);
    expect(parseToolInput('buscar_emails', { ...base, status: ['duplicidade'] }).ok).toBe(true);
    // 'a vencer' e 'protestado' existem em contas, nunca em email_control
    expect(parseToolInput('buscar_emails', { ...base, status: ['a vencer'] }).ok).toBe(false);
    expect(parseToolInput('buscar_emails', { ...base, status: ['protestado'] }).ok).toBe(false);
  });

  it('buscar_emails tem teto de 50 (a tool devolve trecho de PII, não conta agregada)', () => {
    expect(parseToolInput('buscar_emails', { termo: 'nota', limit: 50 }).ok).toBe(true);
    expect(parseToolInput('buscar_emails', { termo: 'nota', limit: 100 }).ok).toBe(false);
  });

  it('rejeita status inventado, mas aceita os nomes reais da dimensão', () => {
    const base = { date_from: '2026-07-01', date_to: '2026-07-31' };
    expect(parseToolInput('gasto_por_periodo', { ...base, status: ['inexistente'] }).ok).toBe(false);
    expect(parseToolInput('gasto_por_periodo', { ...base, status: ['pago', 'cancelado'] }).ok).toBe(true);
  });

  it('rejeita limit acima do teto (o banco também clampa — esta é a 1ª barreira)', () => {
    const base = { date_from: '2026-07-01', date_to: '2026-07-31' };
    expect(parseToolInput('gasto_por_fornecedor', { ...base, limit: 9999 }).ok).toBe(false);
    expect(parseToolInput('gasto_por_fornecedor', { ...base, limit: 100 }).ok).toBe(true);
  });

  // O domínio do Zod tem de bater com o enum do JSON Schema. Aceitar aqui o que lá é proibido faz
  // o modelo receber lista vazia e concluir "não há contas dessa empresa" — em vez de um erro que
  // ele consegue corrigir, que é o propósito desta camada.
  it('rejeita sk_company fora de {1,2,3,4}, o mesmo domínio do JSON Schema', () => {
    expect(parseToolInput('resumo_situacao', { sk_company: 99 }).ok).toBe(false);
    expect(parseToolInput('resumo_situacao', { sk_company: 5 }).ok).toBe(false);
    expect(parseToolInput('resumo_situacao', { sk_company: 3 }).ok).toBe(true);
    // 4 = LE BLANC (2026-09-11): sem ela o modelo não filtraria a empresa nova.
    expect(parseToolInput('resumo_situacao', { sk_company: 4 }).ok).toBe(true);
  });

  it('rejeita nature_id fora da faixa de smallint (evita erro cru do Postgres)', () => {
    const base = { date_from: '2026-07-01', date_to: '2026-07-31', group_by: 'grupo' };
    expect(parseToolInput('gasto_por_classificacao', { ...base, nature_ids: [99_999] }).ok).toBe(false);
    expect(parseToolInput('gasto_por_classificacao', { ...base, nature_ids: [2, 8] }).ok).toBe(true);
  });

  // Onda 3. O domínio de tipo fiscal vive em DUAS camadas: o CASE do SQL (que devolve -1 para
  // valor desconhecido, resultando em conjunto VAZIO) e este Zod. Sem a barreira aqui, "danfe"
  // devolveria zero linhas e o modelo concluiria "não recebemos nenhum" — falso e silencioso.
  it('rejeita tipo de documento fiscal fora do domínio, mas aceita os quatro modelos', () => {
    expect(parseToolInput('documentos_fiscais', { tipo: ['danfe'] }).ok).toBe(false);
    expect(parseToolInput('documentos_fiscais', { tipo: ['cte', 'nfe'] }).ok).toBe(true);
    expect(parseToolInput('documentos_fiscais', { tipo: ['cfe', 'nfce'] }).ok).toBe(true);
  });

  it('documentos_fiscais aceita chamada sem filtro nenhum', () => {
    const r = parseToolInput('documentos_fiscais', {});
    expect(r.ok).toBe(true);
  });

  it('documentos_fiscais recusa numero não-inteiro (a chave não é o número)', () => {
    expect(parseToolInput('documentos_fiscais', { numero: 'AB123' }).ok).toBe(false);
    expect(parseToolInput('documentos_fiscais', { numero: 19016 }).ok).toBe(true);
  });

  it('nunca lança — parâmetro do modelo é fluxo normal, não exceção', () => {
    expect(() => parseToolInput('resumo_situacao', null)).not.toThrow();
    expect(() => parseToolInput('resumo_situacao', 'texto')).not.toThrow();
    expect(parseToolInput('resumo_situacao', null).ok).toBe(false);
  });
});

describe('runTool', () => {
  function clientMock(result: { data?: unknown; error?: { message: string } }) {
    const setHeader = vi.fn().mockResolvedValue(result);
    const rpc = vi.fn(() => ({ setHeader }));
    const schema = vi.fn(() => ({ rpc }));
    return { client: { schema } as never, schema, rpc, setHeader };
  }

  it('usa o schema analytics e repassa o JWT do usuário (a RLS depende disso)', async () => {
    const m = clientMock({ data: [{ status_name: 'pago' }] });
    const rows = await runTool(m.client, 'jwt-do-usuario', 'resumo_situacao', { p_sk_company: 1 });

    expect(m.schema).toHaveBeenCalledWith(ANALYTICS_SCHEMA);
    expect(m.rpc).toHaveBeenCalledWith('resumo_situacao', { p_sk_company: 1 });
    expect(m.setHeader).toHaveBeenCalledWith('Authorization', 'Bearer jwt-do-usuario');
    expect(rows).toEqual([{ status_name: 'pago' }]);
  });

  it('normaliza retorno não-array para lista vazia', async () => {
    const m = clientMock({ data: null });
    await expect(runTool(m.client, 't', 'resumo_situacao', {})).resolves.toEqual([]);
  });

  it('propaga erro do PostgREST para o caller decidir', async () => {
    const m = clientMock({ error: { message: 'permission denied' } });
    await expect(runTool(m.client, 't', 'resumo_situacao', {})).rejects.toThrow(/permission denied/);
  });
});
