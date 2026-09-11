// lib/ai-chat/tools.ts
// As 12 tools do chat de IA: definição enviada ao modelo + execução via RPC no schema `analytics`.
// (6 da migration 098 · demonstrativo_despesas da 104 — Onda 1 · buscar_emails da 106 — Onda 2 ·
// documentos_fiscais da 108 — Onda 3 · auditoria_eventos e auditoria_resumo da 118 — Onda 7 ·
// pontualidade_pagamento da 121 — Onda 9.)
//
// POR QUE JSON SCHEMA CRU E NÃO ZOD (§17.7 do doc de arquitetura)
// O §6 do documento já especifica os contratos em JSON Schema, que é exatamente o formato que a
// Claude API consome. Converter para Zod só para o helper `betaZodTool` reconverter para JSON
// Schema seria trabalho circular — e o projeto está em Zod 4, enquanto o helper foi escrito
// contra Zod 3. O Zod permanece onde de fato agrega: validando os parâmetros que o MODELO produz,
// antes de chegarem ao banco (`parseToolInput` abaixo).
//
// POR QUE CADA TOOL É UMA FUNÇÃO SQL, NÃO UMA VIEW FILTRADA
// O PostgREST só agrega (`sum`/`count`) com `db-aggregates-enabled`, desligado por padrão no
// Supabase. Como função: a agregação roda no banco, os parâmetros viajam como BIND (o gateway
// nunca interpola string do modelo em SQL) e, sendo SECURITY INVOKER, a RLS das tabelas base
// continua valendo com o JWT do usuário.

import { z } from 'zod';
import type { SupabaseClient } from '@supabase/supabase-js';

/** Schema exposto ao PostgREST (migration 098). */
export const ANALYTICS_SCHEMA = 'analytics';

/**
 * Teto de linhas por tool. Espelha o clamp que existe DENTRO das funções SQL — aqui é só o
 * default enviado ao modelo; quem garante o limite é o banco, porque o modelo pode pedir mais.
 */
const DEFAULT_LIMIT = 20;
const DETAIL_LIMIT = 50;

// Domínios espelhados do banco. `status` são os nomes da dimensão (migration 067+); as empresas
// são os 4 `sk_company` reais (1 OTIMOTEX TECIDOS · 2 LEBIANCO · 3 OTIMOTEX FARDOS · 4 LE BLANC).
const STATUS_NAMES = [
  'pendente', 'vencido', 'a vencer', 'prorrogado', 'baixado',
  'protestado', 'cartório', 'pago', 'cancelado', 'falha',
] as const;
// Status de `email_control` (migrations 022/031) — domínio DIFERENTE do de contas acima. São a
// caixa de entrada, não o ciclo de vida do título; misturar os dois faria o modelo filtrar por um
// valor que não existe naquela tabela e receber lista vazia, concluindo "não há e-mails".
const EMAIL_STATUS_NAMES = [
  'extraído', 'recebido', 'pendente', 'falha', 'ignorado', 'duplicidade',
] as const;
// Modelos de documento fiscal (migration 107 — Onda 3). Os nomes viajam em minúsculas e o SQL
// os traduz para o número do modelo (55/57/59/65). Valor fora deste domínio devolve VAZIO no
// banco, nunca "todos" — mas o Zod já o barra antes, com mensagem que o modelo consegue corrigir.
const FISCAL_DOC_TYPES = ['nfe', 'cte', 'cfe', 'nfce'] as const;
// Trilha de auditoria (migrations 117/118 — Onda 7). As tabelas auditadas são só estas duas; um
// nome fora do domínio devolveria VAZIO no banco, e o modelo concluiria "não houve alteração"
// em vez de "essa tabela não é auditada" — daí o enum barrar antes, com mensagem corrigível.
const AUDIT_TABLES = ['financial_account_control', 'supplier'] as const;
const AUDIT_OPERATIONS = ['UPDATE', 'DELETE', 'TRUNCATE'] as const;
const AUDIT_GROUPS = ['usuario', 'campo', 'tabela', 'operacao'] as const;
// Onda 9. 'faixa' agrupa por severidade do atraso; 'geral' devolve uma linha só (o panorama).
const PUNCTUALITY_GROUPS = ['geral', 'fornecedor', 'empresa', 'mes', 'faixa'] as const;
const DATE_FIELDS = ['vencimento', 'pagamento', 'emissao'] as const;
const GRANULARITIES = ['dia', 'semana', 'mes', 'trimestre'] as const;
const CLASSIFICATION_DIMS = ['centro_custo', 'plano_contas', 'grupo', 'subgrupo', 'tipo'] as const;
const AGING_GROUPS = ['faixa', 'empresa', 'fornecedor', 'centro_custo', 'plano_contas'] as const;
const SK_COMPANIES = [1, 2, 3, 4] as const;

const isoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'Data deve ser YYYY-MM-DD');

// O domínio precisa ser o MESMO do JSON Schema enviado ao modelo. Aceitar aqui um valor que lá é
// proibido faz o modelo receber uma lista vazia e concluir "não há contas dessa empresa", em vez
// de um erro que ele consegue corrigir — o oposto do propósito desta camada.
const skCompanyId = z
  .number()
  .int()
  .refine((v): v is (typeof SK_COMPANIES)[number] => (SK_COMPANIES as readonly number[]).includes(v), {
    message: `Empresa inválida — use ${SK_COMPANIES.join(', ')}`,
  });

// Id do catálogo `financial_type_group`, usado por DUAS dimensões distintas: a NATUREZA do grupo
// (nature_ids) e o TIPO do subgrupo (subgroup_type_ids). O nome é genérico de propósito — chamá-lo
// de `natureId` e reusá-lo no tipo do subgrupo induziria justamente a confusão que o system prompt
// adverte. `smallint` no banco: fora da faixa vira erro cru do Postgres no meio do loop, em vez de
// uma mensagem que o modelo entende.
const typeGroupId = z.number().int().min(0).max(32_767);

// Validação dos parâmetros que o MODELO gera. Não substitui as guardas do banco (as funções
// clampam limit e rejeitam group_by fora do domínio) — é a primeira barreira, que devolve um erro
// legível ao modelo em vez de deixá-lo receber um resultado vazio e concluir que "não há dados".
const schemas = {
  resumo_situacao: z.object({
    sk_company: skCompanyId.optional(),
    as_of: isoDate.optional(),
  }),
  gasto_por_periodo: z.object({
    date_from: isoDate,
    date_to: isoDate,
    date_field: z.enum(DATE_FIELDS).optional(),
    granularity: z.enum(GRANULARITIES).optional(),
    status: z.array(z.enum(STATUS_NAMES)).optional(),
    sk_company: skCompanyId.optional(),
  }),
  gasto_por_fornecedor: z.object({
    date_from: isoDate,
    date_to: isoDate,
    date_field: z.enum(DATE_FIELDS).optional(),
    supplier: z.string().optional(),
    sk_company: skCompanyId.optional(),
    limit: z.number().int().min(1).max(100).optional(),
    has_invoice: z.boolean().optional(),
    has_bank_slip: z.boolean().optional(),
  }),
  gasto_por_classificacao: z.object({
    date_from: isoDate,
    date_to: isoDate,
    group_by: z.enum(CLASSIFICATION_DIMS),
    date_field: z.enum(DATE_FIELDS).optional(),
    nature_ids: z.array(typeGroupId).optional(),
    sk_company: skCompanyId.optional(),
    limit: z.number().int().min(1).max(100).optional(),
    subgroup_type_ids: z.array(typeGroupId).optional(),
  }),
  demonstrativo_despesas: z.object({
    date_from: isoDate,
    date_to: isoDate,
    date_field: z.enum(DATE_FIELDS).optional(),
    sk_company: skCompanyId.optional(),
  }),
  aging_vencidos: z.object({
    group_by: z.enum(AGING_GROUPS).optional(),
    sk_company: skCompanyId.optional(),
    limit: z.number().int().min(1).max(100).optional(),
  }),
  listar_contas: z.object({
    date_from: isoDate,
    date_to: isoDate,
    date_field: z.enum(DATE_FIELDS).optional(),
    supplier: z.string().optional(),
    status: z.array(z.enum(STATUS_NAMES)).optional(),
    sk_company: skCompanyId.optional(),
    page: z.number().int().min(1).optional(),
    limit: z.number().int().min(1).max(100).optional(),
    has_invoice: z.boolean().optional(),
    has_bank_slip: z.boolean().optional(),
  }),
  buscar_emails: z.object({
    // O termo é o único obrigatório: buscar "tudo" na caixa não é caso de uso e traria PII sem
    // propósito. `.min(2)` evita que uma letra solta varra a base inteira.
    termo: z.string().trim().min(2, 'Termo de busca muito curto'),
    date_from: isoDate.optional(),
    date_to: isoDate.optional(),
    sender: z.string().optional(),
    status: z.array(z.enum(EMAIL_STATUS_NAMES)).optional(),
    limit: z.number().int().min(1).max(50).optional(),
  }),
  documentos_fiscais: z.object({
    tipo: z.array(z.enum(FISCAL_DOC_TYPES)).optional(),
    emitente: z.string().optional(),
    date_from: isoDate.optional(),
    date_to: isoDate.optional(),
    numero: z.number().int().min(1).optional(),
    // Onda 5: sigla de praça (origem OU destino) ou a rota inteira "CCT->RIO".
    rota: z.string().trim().min(2).max(9).optional(),
    limit: z.number().int().min(1).max(100).optional(),
  }),
  auditoria_eventos: z.object({
    date_from: isoDate.optional(),
    date_to: isoDate.optional(),
    tabela: z.enum(AUDIT_TABLES).optional(),
    campo: z.string().optional(),
    usuario: z.string().optional(),
    operacao: z.enum(AUDIT_OPERATIONS).optional(),
    registro_id: z.number().int().min(1).optional(),
    limit: z.number().int().min(1).max(100).optional(),
  }),
  auditoria_resumo: z.object({
    date_from: isoDate.optional(),
    date_to: isoDate.optional(),
    group_by: z.enum(AUDIT_GROUPS),
    apenas_sensiveis: z.boolean().optional(),
    tabela: z.enum(AUDIT_TABLES).optional(),
    limit: z.number().int().min(1).max(100).optional(),
  }),
  pontualidade_pagamento: z.object({
    // date_from/date_to filtram por DATA DE PAGAMENTO — é a única data que faz sentido aqui, e
    // por isso não há `date_field`: pontualidade de um período é sobre o que foi PAGO nele.
    date_from: isoDate.optional(),
    date_to: isoDate.optional(),
    group_by: z.enum(PUNCTUALITY_GROUPS),
    sk_company: skCompanyId.optional(),
    min_contas: z.number().int().min(1).max(1000).optional(),
    limit: z.number().int().min(1).max(100).optional(),
  }),
} as const;

// Nome válido de tool — contrato de TOOL_DEFINITIONS/isToolName, consumido via inferência.
// ts-prune-ignore-next
export type ToolName = keyof typeof schemas;

/** Definição enviada ao modelo. `input_schema` é JSON Schema puro (formato da Claude API). */
// ts-prune-ignore-next
export interface ToolDefinition {
  name: ToolName;
  description: string;
  input_schema: Record<string, unknown>;
}

const dateField = {
  type: 'string',
  enum: DATE_FIELDS,
  default: 'vencimento',
  description: 'vencimento → due_date | pagamento → payment_date | emissao → issue_date',
};
const skCompany = {
  type: 'integer',
  enum: SK_COMPANIES,
  description: 'Empresa PAGADORA: 1 OTIMOTEX TECIDOS, 2 LEBIANCO, 3 OTIMOTEX FARDOS, 4 LE BLANC. '
    + 'É independente do fornecedor — nunca inferir uma da outra.',
};
const limitProp = (def: number) => ({ type: 'integer', maximum: 100, default: def });

// Curadoria (migration 033): as duas flags que o operador marca no /consulta. Tri-state — omitir
// não filtra. "Boleto sem nota fiscal" é has_bank_slip=true + has_invoice=false, o achado de
// compliance mais material da base.
const hasInvoice = {
  type: 'boolean',
  description: 'Filtra pela flag "Tem NF". Omitir não filtra.',
};
const hasBankSlip = {
  type: 'boolean',
  description: 'Filtra pela flag "Tem Boleto". Omitir não filtra.',
};

// ts-prune-ignore-next — consumido pelo gateway por inferência.
export const TOOL_DEFINITIONS: readonly ToolDefinition[] = [
  {
    name: 'resumo_situacao',
    description:
      'KPIs por situação: quantidade e valor em cada status. Use para "como estamos", '
      + '"quanto tenho a pagar". Exclui cancelado. Devolve também overdue_count/overdue_amount, '
      + 'que recalculam o vencido por data de vencimento — use esses para "quanto está vencido", '
      + 'porque o rótulo de status é atualizado por batch diário e fica defasado.',
    input_schema: {
      type: 'object',
      properties: {
        sk_company: skCompany,
        as_of: { type: 'string', format: 'date', description: 'Data de referência (padrão: hoje).' },
      },
    },
  },
  {
    name: 'gasto_por_periodo',
    description:
      'Total agregado por período (série temporal). Use para "quanto paguei/devo em X", '
      + 'evolução mensal, comparação entre meses. '
      + '🔴 CADA BALDE DECLARA SE ESTÁ INCOMPLETO. Quando `is_partial` for verdadeiro, o balde '
      + 'cobre apenas `days_covered` dos `days_total` dias — porque começa antes do início do '
      + 'período pedido, ou porque termina no FUTURO e os dias que faltam ainda não aconteceram. '
      + 'NUNCA compare um balde parcial com um completo sem dizer isso: uma semana de 4 dias ao '
      + 'lado de uma de 7 parece "queda de 50%" e não é. Ao citar um balde parcial, diga quantos '
      + 'dias ele tem (ex.: "a semana corrente, com 4 dos 7 dias"); se a pergunta for de '
      + 'comparação, prefira comparar períodos de mesmo tamanho ou avise que o último está em '
      + 'curso. O primeiro e o último balde da série são os que costumam vir parciais. '
      + 'Em date_field="vencimento" o dia futuro NÃO torna o balde parcial — ali são contas a '
      + 'vencer, que já existem na base.',
    input_schema: {
      type: 'object',
      properties: {
        date_from: { type: 'string', format: 'date' },
        date_to: { type: 'string', format: 'date' },
        date_field: dateField,
        granularity: { type: 'string', enum: GRANULARITIES, default: 'mes' },
        status: {
          type: 'array',
          items: { type: 'string', enum: STATUS_NAMES },
          description: 'Sem este filtro, cancelado é excluído. Informá-lo o inclui se pedido.',
        },
        sk_company: skCompany,
      },
      required: ['date_from', 'date_to'],
    },
  },
  {
    name: 'gasto_por_fornecedor',
    description:
      'Ranking/total por fornecedor. Use para "top fornecedores", "quanto paguei para X". '
      + 'O parâmetro supplier casa CNPJ exato ou parte do nome (sem acento/caixa). '
      + 'Com has_bank_slip=true e has_invoice=false responde "quais fornecedores têm boleto '
      + 'sem nota fiscal" (risco de compliance).',
    input_schema: {
      type: 'object',
      properties: {
        date_from: { type: 'string', format: 'date' },
        date_to: { type: 'string', format: 'date' },
        date_field: dateField,
        supplier: { type: 'string', description: 'Nome parcial ou CNPJ.' },
        sk_company: skCompany,
        limit: limitProp(DEFAULT_LIMIT),
        has_invoice: hasInvoice,
        has_bank_slip: hasBankSlip,
      },
      required: ['date_from', 'date_to'],
    },
  },
  {
    name: 'gasto_por_classificacao',
    description:
      'Agrega pela classificação contábil. Use para "gasto por centro de custo", '
      + '"por plano de contas", "por grupo/subgrupo" e — com group_by="tipo" — para '
      + '"quanto foi despesa FIXA vs VARIÁVEL".',
    input_schema: {
      type: 'object',
      properties: {
        date_from: { type: 'string', format: 'date' },
        date_to: { type: 'string', format: 'date' },
        group_by: {
          type: 'string',
          enum: CLASSIFICATION_DIMS,
          description: 'tipo = Despesas Fixas / Despesas Variáveis / Custos de Mercadorias.',
        },
        date_field: dateField,
        nature_ids: {
          type: 'array',
          items: { type: 'integer' },
          description: 'Natureza do GRUPO: 2 = Despesas, 8 = Custo, 4 = Passivo (tributos).',
        },
        subgroup_type_ids: {
          type: 'array',
          items: { type: 'integer' },
          description: 'Tipo do SUBGRUPO: 5 = Despesas Fixas, 6 = Despesas Variáveis, '
            + '7 = Custos de Mercadorias, 9 = Custos de Importações. Dimensão distinta de '
            + 'nature_ids — não confundir.',
        },
        sk_company: skCompany,
        limit: limitProp(DEFAULT_LIMIT),
      },
      required: ['date_from', 'date_to', 'group_by'],
    },
  },
  {
    name: 'demonstrativo_despesas',
    description:
      'Demonstrativo de Custos e Despesas do período: uma linha por tipo cadastrado no catálogo '
      + 'financeiro (hoje inclui Custos de Mercadorias, Custos de Importação, Despesas Fixas, '
      + 'Despesas Variáveis e Tributos), mais Não classificado e o Total de saídas. 🔴 A LISTA DE '
      + 'LINHAS É DINÂMICA (migration 128) — um tipo novo cadastrado no catálogo pode aparecer '
      + 'aqui SEM aviso prévio nesta descrição; não assuma que só estas linhas existem, confie no '
      + 'que a tool devolver. '
      + 'Use para "demonstrativo", "estrutura de custos", "para onde foi o dinheiro". '
      + 'As linhas são mutuamente exclusivas e exaustivas, então SEMPRE somam o total — NÃO '
      + 'recalcule o total, use a linha "Total de saídas" que a tool devolve. '
      + 'NÃO é um DRE: este sistema é contas a pagar e não tem receitas; se perguntarem por DRE, '
      + 'explique isso e ofereça este demonstrativo.',
    input_schema: {
      type: 'object',
      properties: {
        date_from: { type: 'string', format: 'date' },
        date_to: { type: 'string', format: 'date' },
        date_field: dateField,
        sk_company: skCompany,
      },
      required: ['date_from', 'date_to'],
    },
  },
  {
    name: 'aging_vencidos',
    description:
      'Aging dos títulos EM ABERTO já vencidos, por faixa (1-30, 31-60, 61-90, 90+). '
      + 'Calculado por data de vencimento, não pelo rótulo de status.',
    input_schema: {
      type: 'object',
      properties: {
        group_by: { type: 'string', enum: AGING_GROUPS, default: 'faixa' },
        sk_company: skCompany,
        limit: limitProp(DETAIL_LIMIT),
      },
    },
  },
  {
    name: 'listar_contas',
    description:
      'Drill-down: lista as contas individuais de um recorte, paginado. Use DEPOIS de um '
      + 'agregado, quando o usuário pedir o detalhe ("quais são essas contas?"). '
      + 'Com has_bank_slip=true e has_invoice=false lista as contas com boleto e SEM nota fiscal.',
    input_schema: {
      type: 'object',
      properties: {
        date_from: { type: 'string', format: 'date' },
        date_to: { type: 'string', format: 'date' },
        date_field: dateField,
        supplier: { type: 'string' },
        status: { type: 'array', items: { type: 'string', enum: STATUS_NAMES } },
        sk_company: skCompany,
        page: { type: 'integer', minimum: 1, default: 1 },
        limit: limitProp(DETAIL_LIMIT),
        has_invoice: hasInvoice,
        has_bank_slip: hasBankSlip,
      },
      required: ['date_from', 'date_to'],
    },
  },
  {
    name: 'buscar_emails',
    description:
      'Busca textual nos E-MAILS RECEBIDOS (assunto + corpo), não nas contas. Use quando a '
      + 'pergunta for sobre o que foi ESCRITO numa mensagem — "em qual e-mail falaram em '
      + 'reajuste?", "algum fornecedor avisou de mudança de conta bancária?". Devolve um trecho '
      + 'com o termo destacado entre << >>, não o e-mail inteiro. '
      + 'ATENÇÃO: e-mails antigos podem ter o corpo incompleto (só foi guardado por inteiro a '
      + 'partir de 31/07/2026) e e-mails fora do filtro de assunto não têm corpo algum — se a '
      + 'busca não achar, isso não prova que o assunto nunca foi mencionado.',
    input_schema: {
      type: 'object',
      properties: {
        termo: { type: 'string', description: 'Palavra ou expressão a procurar.' },
        date_from: { type: 'string', format: 'date' },
        date_to: { type: 'string', format: 'date' },
        sender: { type: 'string', description: 'Parte do e-mail do remetente.' },
        status: {
          type: 'array',
          items: { type: 'string', enum: EMAIL_STATUS_NAMES },
          description: 'Situação do E-MAIL (não da conta): extraído, recebido, pendente, falha, '
            + 'ignorado, duplicidade.',
        },
        limit: { type: 'integer', maximum: 50, default: DEFAULT_LIMIT },
      },
      required: ['termo'],
    },
  },
  {
    name: 'documentos_fiscais',
    description:
      'Documentos fiscais eletrônicos RECEBIDOS por e-mail (CT-e de frete, NF-e de mercadoria, '
      + 'CF-e, NFC-e), identificados pela chave de acesso de 44 dígitos. Responde "quantos CT-e '
      + 'a transportadora X emitiu?", "recebi a NF-e número 19016?", "de quais emitentes vieram '
      + 'conhecimentos de transporte em julho?". '
      + 'Para parte dos CT-e traz também o CONTEÚDO do transporte: rota, peso, nota fiscal '
      + 'transportada, destinatário e valor do frete — responde "quanto custou o frete para o '
      + 'Rio?", "qual rota pesa mais?", "qual NF veio neste conhecimento?". '
      + '🔴 DOCUMENTO FISCAL NÃO É CONTA A PAGAR. O frete já entra no sistema como BOLETO e a '
      + 'NF-e é a origem da mercadoria, não a obrigação de pagamento. NUNCA misture estes '
      + 'documentos com gastos nem os apresente como despesa. '
      + '🔴 `frete_rs` é a DECOMPOSIÇÃO da fatura que JÁ ESTÁ lançada como conta a pagar — o '
      + 'mesmo dinheiro visto por rota. Serve para repartir o frete entre rotas/destinatários, '
      + 'JAMAIS para somar com gasto_por_periodo, gasto_por_fornecedor ou '
      + 'demonstrativo_despesas: isso contaria a mesma despesa duas vezes. '
      + 'O conteúdo existe hoje só para os CT-e que vieram em FATURA AGREGADA da transportadora; '
      + 'nos demais esses campos vêm vazios, o que significa "ainda não extraído", não "não tem". '
      + 'Cada linha traz total_encontrado com a contagem REAL do filtro (antes do limite) — use '
      + 'esse número para contar, nunca o número de linhas devolvidas. '
      + 'COBERTURA PARCIAL: só documentos cujo PDF chegou por e-mail e ainda estava no bucket; '
      + 'não achar um documento não prova que ele não existiu.',
    input_schema: {
      type: 'object',
      properties: {
        tipo: {
          type: 'array',
          items: { type: 'string', enum: FISCAL_DOC_TYPES },
          description: 'cte → conhecimento de transporte · nfe → nota fiscal de mercadoria · '
            + 'cfe → cupom fiscal eletrônico · nfce → NFC-e. Omitir traz todos.',
        },
        emitente: {
          type: 'string',
          description: 'CNPJ (com ou sem máscara) OU parte do nome do emitente, quando ele já '
            + 'estiver cadastrado como fornecedor.',
        },
        date_from: { type: 'string', format: 'date' },
        date_to: { type: 'string', format: 'date' },
        numero: { type: 'integer', description: 'Número do documento (não a chave de acesso).' },
        rota: {
          type: 'string',
          description: 'Praça de ORIGEM ou de DESTINO (sigla da transportadora, ex.: "RIO", '
            + '"MGF", "CCT"), ou a rota inteira no formato "CCT->RIO". Casa qualquer uma das '
            + 'duas pontas, porque "frete para o Rio" é destino e "saindo de SP" é origem.',
        },
        limit: limitProp(DEFAULT_LIMIT),
      },
    },
  },
  {
    name: 'auditoria_eventos',
    description:
      'Trilha de auditoria: QUEM alterou o QUÊ, e quando, em contas a pagar e fornecedores. '
      + 'Use para "quem mudou o valor desta conta?", "o que aconteceu com a conta 794?", '
      + '"alguém alterou a chave PIX de algum fornecedor?", "quem excluiu contas neste mês?". '
      + 'Devolve o DELTA de cada alteração (valor antes → depois), não a linha inteira. '
      + '🔴 A trilha COMEÇA em 11/08/2026: não encontrar evento anterior a essa data NÃO prova '
      + 'que nada mudou antes — antes disso o sistema só guardava o ÚLTIMO editor de cada conta. '
      + '🔴 ator_via="servico" significa alteração de ROTINA AUTOMÁTICA (pipeline de e-mail, '
      + 'batch diário) ou edição não atribuível — NUNCA leia como "ninguém alterou". '
      + 'O campo `usuario` tem TRÊS formas, que não devem ser confundidas: um e-mail (a pessoa), '
      + '"(automacao / nao atribuivel)" (rotina automática) e "(usuario removido: <id>)" — este '
      + 'último foi uma AÇÃO HUMANA cuja conta depois foi apagada, e não uma automação. '
      + 'O filtro por campo inclui a EXCLUSÃO do registro, porque apagar a conta destrói aquele '
      + 'campo junto: "quem mexeu no valor?" precisa mostrar também quem excluiu a conta. '
      + 'Cada linha traz total_encontrado com a contagem REAL do filtro (antes do limite) — use '
      + 'esse número para contar, nunca o número de linhas devolvidas.',
    input_schema: {
      type: 'object',
      properties: {
        date_from: { type: 'string', format: 'date' },
        date_to: { type: 'string', format: 'date' },
        tabela: {
          type: 'string',
          enum: AUDIT_TABLES,
          description: 'financial_account_control = contas a pagar · supplier = fornecedores. '
            + 'São as únicas tabelas auditadas.',
        },
        campo: {
          type: 'string',
          description: 'Nome da coluna alterada, ex.: amount (valor), due_date (vencimento), '
            + 'status_id (situação), sk_supplier (fornecedor), barcode, pix_key1.',
        },
        usuario: {
          type: 'string',
          description: 'Parte do e-mail de quem fez a alteração. Casa também "removido" para '
            + 'achar ações de usuários cuja conta foi apagada.',
        },
        operacao: { type: 'string', enum: AUDIT_OPERATIONS },
        registro_id: {
          type: 'integer',
          description: 'Id da conta (ou sk_supplier) para ver o histórico de UM registro.',
        },
        limit: limitProp(DETAIL_LIMIT),
      },
    },
  },
  {
    name: 'auditoria_resumo',
    description:
      'Agregado da trilha de auditoria: quantas alterações por usuário, por campo, por tabela ou '
      + 'por operação. É a tool para "quais usuários vêm alterando campos sensíveis" '
      + '(group_by="usuario" + apenas_sensiveis=true), "qual campo mais muda", "quem mexeu mais '
      + 'no cadastro este mês". Prefira-a a listar eventos quando a pergunta for de VOLUME ou '
      + 'de RANKING — a lista de eventos é truncada e contá-la daria número errado. '
      + 'Campos sensíveis = valor, valor cobrado, vencimento, data de pagamento, situação, '
      + 'fornecedor, empresa, código de barras, nosso número, nº do documento, flags de NF/Boleto, '
      + 'classificação contábil e, no fornecedor, chave PIX e CNPJ/CPF. '
      + 'Mesma ressalva de cobertura da auditoria_eventos: a trilha começa em 11/08/2026.',
    input_schema: {
      type: 'object',
      properties: {
        date_from: { type: 'string', format: 'date' },
        date_to: { type: 'string', format: 'date' },
        group_by: {
          type: 'string',
          enum: AUDIT_GROUPS,
          description: 'usuario = quem alterou — "(automacao / nao atribuivel)" agrupa as rotinas '
            + 'automáticas e "(usuario removido: <id>)" é uma pessoa cuja conta foi apagada, '
            + 'NUNCA some as duas · campo = qual coluna (só alterações de campo; a EXCLUSÃO de '
            + 'registro não aparece neste eixo — para vê-la use group_by="operacao") · tabela.',
        },
        apenas_sensiveis: {
          type: 'boolean',
          default: false,
          description: 'true = conta só as alterações que tocaram campo sensível.',
        },
        tabela: { type: 'string', enum: AUDIT_TABLES },
        limit: limitProp(DEFAULT_LIMIT),
      },
      required: ['group_by'],
    },
  },
  {
    name: 'pontualidade_pagamento',
    description:
      'Pontualidade de pagamento: quanto se paga EM DIA, com que atraso e onde o atraso se '
      + 'concentra. Responde "estamos pagando em dia?", "qual o atraso médio?", "quais '
      + 'fornecedores recebem com atraso?", "a pontualidade melhorou de um mês para o outro?". '
      + '🔴 NÃO é DPO. DPO é um indicador contábil (saldo de contas a pagar ÷ CMV × dias) e exige '
      + 'o passivo da empresa; aqui a base é só o que chegou por e-mail. Nunca apresente este '
      + 'resultado como DPO nem como prazo médio de pagamento a fornecedores. '
      + '🔴 COBERTURA PARCIAL, e ela precisa ser dita: só entram contas pagas a partir de '
      + '`cobertura_desde`. Antes dessa data a data de pagamento veio de um backfill (ficou igual '
      + 'ao vencimento) e produziria atraso ZERO artificial. `fora_da_cobertura` diz quantas '
      + 'contas pagas do período ficaram de fora por isso, e `excluidas_venc_alterado` quantas '
      + 'saíram porque o vencimento foi alterado DEPOIS do pagamento (nesses casos o atraso '
      + 'mediria a alteração, não o pagamento). Cite esses números quando forem maiores que zero. '
      + '`pct_pontualidade` conta como pontual o que foi pago ATÉ o vencimento (antecipado '
      + 'inclusive). `atraso_medio_dias` e `atraso_mediano_dias` somam APENAS as atrasadas e vêm '
      + 'vazios quando não houve nenhuma — não os leia como zero; `desvio_medio_dias` é o líquido '
      + 'com sinal, em que antecipação abate atraso. '
      + '🔴 Quando o período consultado só tiver contas fora da cobertura, a resposta vem com UMA '
      + 'linha de aviso ("nenhuma conta com data de pagamento confiável no período"), contas = 0 e '
      + '`fora_da_cobertura` maior que zero. Isso NÃO significa que não houve pagamento no '
      + 'período — significa que houve e não é mensurável. Diga isso ao usuário, com o número. '
      + 'Cada linha traz total_encontrado com a contagem REAL antes do limite — use esse número '
      + 'para contar, nunca o número de linhas devolvidas. '
      + '🔴 Em group_by="mes", cada linha declara se o MÊS está incompleto: `mes_parcial`, com '
      + '`dias_cobertos` de `dias_totais`. O primeiro mês costuma ser cortado pelo início da '
      + 'cobertura e o último pelo presente — hoje o mês do corte tem 3 dos 31 dias, e mesmo '
      + 'assim vem rotulado "2026-07", que se lê como julho inteiro. `contas` e `valor_total` de '
      + 'um mês de 3 dias NÃO se comparam com os de um mês cheio: dizer "agosto teve 5x mais '
      + 'contas que julho" seria falso. Ao citar um mês parcial, diga quantos dias ele cobre. '
      + 'As razões (`pct_pontualidade`, `atraso_medio_dias`) não encolhem com o número de dias, '
      + 'mas ficam ruidosas em amostra pequena — cite `contas` junto. Nos demais eixos as três '
      + 'colunas vêm vazias: ali o grupo não delimita período.',
    input_schema: {
      type: 'object',
      properties: {
        date_from: {
          type: 'string',
          format: 'date',
          description: 'Início do período, pela DATA DE PAGAMENTO (não vencimento).',
        },
        date_to: { type: 'string', format: 'date' },
        group_by: {
          type: 'string',
          enum: PUNCTUALITY_GROUPS,
          description: 'geral = uma linha, o panorama · faixa = onde o atraso se concentra '
            + '(antecipado, em dia, 1-7, 8-30, 31+ dias) · fornecedor · empresa · mes.',
        },
        sk_company: { type: 'integer', enum: [1, 2, 3] },
        min_contas: {
          type: 'integer',
          minimum: 1,
          description: 'Descarta grupos com menos de N contas. Use em group_by="fornecedor": '
            + 'pontualidade calculada sobre 1 conta é ruído, não indicador.',
        },
        limit: limitProp(DEFAULT_LIMIT),
      },
      required: ['group_by'],
    },
  },
] as const;

const TOOL_NAMES = new Set<string>(TOOL_DEFINITIONS.map((t) => t.name));

export function isToolName(name: string): name is ToolName {
  return TOOL_NAMES.has(name);
}

/**
 * Valida o input gerado pelo modelo e converte para os parâmetros nomeados da função SQL
 * (prefixo `p_`, conforme a migration 098).
 *
 * @returns `{ ok: true, params }` ou `{ ok: false, message }` — a mensagem volta ao MODELO como
 *   `tool_result` com `is_error: true`, para ele corrigir a chamada. Nunca vira exceção: um
 *   parâmetro errado do modelo é fluxo normal do loop, não falha da requisição.
 */
export function parseToolInput(
  name: ToolName,
  raw: unknown,
): { ok: true; params: Record<string, unknown> } | { ok: false; message: string } {
  const parsed = schemas[name].safeParse(raw);
  if (!parsed.success) {
    const detalhe = parsed.error.issues
      .map((i) => `${i.path.join('.') || '(raiz)'}: ${i.message}`)
      .join('; ');
    return { ok: false, message: `Parâmetros inválidos para ${name} — ${detalhe}` };
  }
  // O Zod já removeu chaves desconhecidas (strip). Prefixa para casar a assinatura SQL.
  const params: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(parsed.data)) {
    if (value !== undefined) params[`p_${key}`] = value;
  }
  return { ok: true, params };
}

/**
 * Executa a tool via RPC no schema `analytics`, com o JWT do PRÓPRIO usuário.
 *
 * O `client` precisa ser o anon (`getAnonClient`), nunca o service_role: é o que faz a RLS da
 * migration 076 decidir o recorte — um usuário do grupo Comercial só enxerga as contas em que é
 * dono, exatamente como na tela. Com service_role o chat veria tudo.
 *
 * `.schema(ANALYTICS_SCHEMA)` faz o supabase-js enviar `Content-Profile: analytics`, que é o
 * header que seleciona o schema em POST /rpc (o `Accept-Profile` NÃO funciona aqui — verificado
 * contra o PostgREST real, que sem ele procura a função em `public` e devolve PGRST202).
 */
export async function runTool(
  client: SupabaseClient,
  token: string,
  name: ToolName,
  params: Record<string, unknown>,
): Promise<unknown[]> {
  const { data, error } = await client
    .schema(ANALYTICS_SCHEMA)
    .rpc(name, params)
    .setHeader('Authorization', `Bearer ${token}`);

  if (error) throw new Error(`${name}: ${error.message}`);
  return Array.isArray(data) ? data : [];
}
