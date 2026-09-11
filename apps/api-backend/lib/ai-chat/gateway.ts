// lib/ai-chat/gateway.ts
// Orquestrador do chat: monta o contexto, roda o loop de tool use com a Claude API e executa as
// tools contra o schema `analytics` com o JWT do usuário.
//
// NÃO é Repository → Service → Route (§17.7). Os CRUDs seguem aquele padrão porque são RECURSOS;
// isto é uma máquina de estados. Forçá-lo no molde de CRUD produziria um "service" que é só um
// loop, e o que precisa ser testável isoladamente (definição das tools, tradução de erro, log)
// já está em arquivos próprios.

import Anthropic from '@anthropic-ai/sdk';
import type { SupabaseClient } from '@supabase/supabase-js';
import { TOOL_DEFINITIONS, isToolName, parseToolInput, runTool } from './tools';
import { AiChatAbortedError, attachPartialRun, translateAnthropicError } from './errors';
import { CONFIGURED_MODEL } from './model';
import type { LoggedToolCall } from './log';


/**
 * Teto de iterações do loop (§17.2 — não remover).
 *
 * Sem ele, um modelo que se convença de precisar de "mais uma consulta" itera até a function
 * morrer por timeout, com custo proporcional — e o "limite de custo por sessão" prometido no §8
 * não existiria de fato. 6 é folgado para o uso real (um agregado + um drill-down são 2), e
 * atingi-lo é resposta honesta, não exceção.
 */
const MAX_ITERATIONS = 6;

/**
 * Teto de saída por turno. Cobre thinking + texto: tanto no Opus 5 quanto no Sonnet 5 o thinking
 * adaptativo está LIGADO por padrão (omitir o parâmetro não o desliga) e consome deste orçamento
 * (§17.5). Dimensionar apertado trunca a resposta no meio.
 */
const MAX_TOKENS = 8192;

/** Timeout da chamada ao modelo. Menor que o `maxDuration` da rota, para o erro ser nosso. */
const REQUEST_TIMEOUT_MS = Number(process.env.ANTHROPIC_TIMEOUT_MS ?? 120_000);

/**
 * Usado quando o turno termina sem nenhum bloco de texto — acontece se o corte por `max_tokens`
 * cai dentro do raciocínio. Devolver string vazia daria um balão em branco na tela.
 */
const SEM_RESPOSTA = 'Não consegui formular uma resposta. Tente reformular a pergunta.';

/**
 * As tools no formato da API, montadas UMA vez. A ordem e o conteúdo precisam ser idênticos entre
 * as chamadas do mesmo turno: reordenar ou alterar definição invalida os três níveis de cache.
 */
const toolParams: Anthropic.ToolUnion[] = TOOL_DEFINITIONS.map((t) => ({
  name: t.name,
  description: t.description,
  input_schema: t.input_schema as Anthropic.Tool.InputSchema,
}));

// Contrato de entrada/saída de runChat — consumido pela rota via inferência.
// ts-prune-ignore-next
export interface ChatRequest {
  question: string;
  /** Histórico curto (pares user/assistant já normalizados). */
  history?: { role: 'user' | 'assistant'; content: string }[];
}

// ts-prune-ignore-next
export interface ChatResult {
  answer: string;
  toolCalls: LoggedToolCall[];
  rowCount: number;
  /**
   * ATENÇÃO: `input_tokens` da API é só o **resto não-cacheado**. O tamanho real do prompt é
   * `inputTokens + cacheCreationTokens + cacheReadTokens` — por isso os três são registrados.
   * Sem `cacheReadTokens` não há como notar um invalidador silencioso do cache, que não produz
   * erro nenhum: só a conta subindo.
   */
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
  /** true quando a resposta pode estar incompleta: teto de iterações OU corte por `max_tokens`. */
  truncated: boolean;
  /**
   * Chamadas ao modelo DENTRO do loop (a de fechamento não conta). Auditado desde a migration 102:
   * `iterations === MAX_ITERATIONS` é a assinatura da pergunta que estourou o teto — a mais cara e
   * a mais informativa (§11: candidata a tool nova), antes indistinguível de um run limpo.
   */
  iterations: number;
  /**
   * Modelo que SERVIU o turno (`response.model`), não o pedido — auditado desde a migration 129.
   *
   * A distinção não é preciosismo: o pedido é um alias que a API resolve, e com `fallbacks`
   * server-side um turno recusado é reexecutado em OUTRO modelo e responde 200 — registrar o pedido
   * gravaria justamente o modelo que NÃO respondeu.
   *
   * Nunca vazio: sem nenhuma resposta (falha na 1ª iteração, cancelamento antes do 1º token) cai
   * para `CONFIGURED_MODEL`. É o que dá a `model IS NULL` no banco um sentido único — "linha
   * anterior à 129" — em vez de confundir-se com "não sei".
   */
  model: string;
}

/**
 * Observação LATERAL do turno, para quem quiser mostrar progresso enquanto ele acontece.
 *
 * 🔴 O desenho é deliberado: **não existe um segundo `runChat` para streaming**. O loop de tool use
 * é o coração do gateway — duplicá-lo para servir a rota SSE criaria duas cópias do teto de
 * iterações, do pareamento tool_use/tool_result, do acumulador de tokens e da tradução de erro, e
 * elas passariam a divergir na primeira alteração. Aqui o streaming é apenas uma **observação** do
 * mesmo loop: sem `events`, o comportamento é byte a byte o de antes (é o que a suíte existente do
 * gateway continua provando).
 *
 * Todo callback é best-effort: uma exceção lançada por quem observa NUNCA pode derrubar o turno —
 * ver `safeEmit`. Mesmo princípio de `logInteraction`, que nunca lança, e de `attachPartialRun`,
 * que engole a própria falha.
 */
// ts-prune-ignore-next
export interface ChatProgress {
  /** Uma ferramenta vai executar. */
  onToolStart?: (call: { name: string; params: Record<string, unknown> }) => void;
  /** A ferramenta terminou (com `error` quando falhou — o loop segue, por contrato). */
  onToolEnd?: (call: { name: string; rows: number; ms: number; error?: string }) => void;
  /**
   * Uma NOVA mensagem do assistente começou a emitir texto.
   *
   * Emitido uma vez por mensagem, não uma vez por turno: o modelo costuma escrever um preâmbulo,
   * pedir a ferramenta e só então redigir a resposta. Quem observa deve DESCARTAR o texto acumulado
   * a cada sinal destes — é o que mantém a tela igual ao `answer` final, que é apenas o texto da
   * última mensagem (`extractText` da resposta que encerra o loop).
   */
  onTextStart?: () => void;
  /** Um pedaço de texto recém-gerado. */
  onTextDelta?: (text: string) => void;
}

let client: Anthropic | null = null;

function getAnthropic(): Anthropic {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    throw new Error('ANTHROPIC_API_KEY não configurada no ambiente do servidor');
  }
  // Lazy + cacheado pelo mesmo motivo de getAnonClient: não lançar durante o build.
  client ??= new Anthropic({ apiKey, timeout: REQUEST_TIMEOUT_MS, maxRetries: 1 });
  return client;
}

/**
 * Bloco ESTÁVEL do contexto: instruções + dicionário de dados. É o alvo do prompt caching.
 *
 * A DATA DE HOJE NÃO ENTRA AQUI (§17.4 — não regredir). Perguntas como "quanto paguei este mês"
 * exigem que o modelo saiba a data corrente, e a tentação é colocá-la no dicionário. Se ela
 * entrar neste bloco, o prefixo muda a cada requisição e NADA cacheia — silenciosamente, sem
 * erro. A data vai na mensagem do usuário, depois do último breakpoint.
 */
const SYSTEM_PROMPT = `Você é um assistente de análise financeira do sistema de contas a pagar da Otimotex.
Responde perguntas sobre contas a pagar consultando o banco através das ferramentas disponíveis.

## Regras de resposta

- Responda SEMPRE em português do Brasil, de forma direta e objetiva.
- Valores monetários em reais, formato brasileiro: R$ 1.234,56.
- Use as ferramentas para obter números. NUNCA invente ou estime valores.
- Se as ferramentas não conseguirem responder, diga isso claramente em vez de aproximar.
- Ao apresentar vários itens, prefira uma tabela markdown enxuta.
- Não repita o número que acabou de mostrar em prosa logo depois; comente o que ele significa.
- 🔴 **NÃO ENUMERE MAIS DE 15 LINHAS.** Com mais que isso, mostre as 15 mais relevantes (as de
  maior valor, ou as mais próximas do vencimento — o que a pergunta pedir), diga quantas existem
  no total e ofereça recortar ("posso listar as de uma empresa ou de um período"). Uma tabela de
  cem linhas não é lida por ninguém no chat, e cada linha custa tempo de resposta: uma listagem
  exaustiva leva o turno de 10 s para quase um minuto. Isso vale para a LISTAGEM, nunca para a
  RESSALVA — cobertura, balde parcial, total truncado e afins são uma frase e continuam
  obrigatórios.
- Seja econômico no texto em volta dos números: sem repetir a pergunta, sem recapitular o que já
  está na tabela, sem fechamento cerimonioso.

## Dicionário de dados

**Situações** (10): pendente, vencido, a vencer, prorrogado, baixado, protestado, cartório,
pago, cancelado, falha.
- "Em aberto" = pendente + vencido + a vencer.
- **cancelado fica FORA de todos os totais por padrão** (mesma convenção da tela /consulta).
- O rótulo "vencido" é atualizado por um batch diário e fica DEFASADO. Para "o que está
  vencido", use \`aging_vencidos\` ou os campos overdue_* de \`resumo_situacao\`, que calculam
  pela data de vencimento.

**Empresas pagadoras** (sk_company): 1 = OTIMOTEX TECIDOS, 2 = LEBIANCO, 3 = OTIMOTEX FARDOS,
4 = LE BLANC. A empresa pagadora é INDEPENDENTE do fornecedor — pode existir conta da LEBIANCO
cujo fornecedor é a OTIMOTEX, e a LE BLANC pode ser pagadora e/ou fornecedora. Nunca infira uma
a partir da outra.

**Datas**: vencimento (due_date), pagamento (payment_date, quando a conta foi baixada) e
emissão (issue_date). Para "quanto paguei", use pagamento; para "quanto tenho a pagar",
vencimento.

**Classificação contábil**: centro de custo, plano de contas, grupo e subgrupo. O id 0 significa
"não informado" — aparece como tal nos resultados.

**Despesas fixas × variáveis**: a classificação vive no SUBGRUPO do plano de contas e tem quatro
valores — Despesas Fixas, Despesas Variáveis, Custos de Mercadorias e Custos de Importação. Use
\`gasto_por_classificacao\` com group_by="tipo". Não confunda com a NATUREZA do grupo
(2 = Despesas, 8 = Custo, 4 = Passivo/tributos), que é outra dimensão.

**Curadoria de documentos**: cada conta tem duas flags marcadas pelo operador — "Tem NF"
(has_invoice) e "Tem Boleto" (has_bank_slip). Conta com boleto e SEM nota fiscal é risco de
compliance (passivo tributário e possibilidade de boleto falso): responda com
has_bank_slip=true + has_invoice=false.

**Demonstrativo**: use \`demonstrativo_despesas\` para a estrutura de saídas do período. Ele já
devolve a linha "Total de saídas" — use-a, não some as linhas você mesmo.

**Juros, multas e descontos** vêm de \`gasto_por_fornecedor\`, nas colunas fine_interest
(juros + acréscimos), discount (descontos + deduções) e amount_charged (valor efetivamente
cobrado no boleto). Atenção: amount é o valor do DOCUMENTO e amount_charged o do BOLETO — a
diferença entre eles é o que a tesouraria perdeu ou ganhou.

⚠️ **\`gasto_por_fornecedor\` é um RANKING truncado** (top N por valor, no máximo 100 de 165
fornecedores). NUNCA some as linhas dele para obter um total do período — o resultado seria menor
que o real, e silenciosamente. Para totais use \`gasto_por_periodo\` ou \`demonstrativo_despesas\`;
para um fornecedor específico, passe o parâmetro supplier.

**Não existe DRE neste sistema.** Ele é contas a PAGAR e não registra receitas, então não há
Receita Bruta, Lucro Bruto nem Resultado. Se pedirem DRE, diga isso com clareza e ofereça o
Demonstrativo de Custos e Despesas.

**Busca em e-mails** (\`buscar_emails\`): procura no assunto e no corpo das mensagens RECEBIDAS —
é outra base, não as contas. Use quando a pergunta for sobre o que foi ESCRITO numa mensagem.

⚠️ **Ausência de resultado NÃO prova ausência do fato.** A cobertura do corpo é parcial: o texto
completo só passou a ser guardado em 31/07/2026, os e-mails anteriores podem ter só os primeiros
500 caracteres, e as mensagens fora do filtro de assunto não têm corpo algum. Se a busca não achar,
diga que não encontrou **nos e-mails com corpo disponível** — nunca afirme que o assunto nunca foi
mencionado.

**Documentos fiscais** (\`documentos_fiscais\`): CT-e (frete), NF-e (mercadoria), CF-e e NFC-e
recebidos por e-mail, identificados pela chave de acesso de 44 dígitos.

🔴 **Documento fiscal NUNCA é despesa e NUNCA soma com contas a pagar.** O frete já entra no
sistema como BOLETO e a NF-e é a origem da mercadoria, não a obrigação de pagamento — somar os
dois duplicaria o gasto. Esses documentos respondem "o que foi emitido/recebido", jamais "quanto
foi gasto".

Parte dos CT-e traz o **conteúdo do transporte** (rota, peso, NF transportada, destinatário e
\`frete_rs\`), extraído da fatura agregada da transportadora.

🔴 **\`frete_rs\` é DECOMPOSIÇÃO, não uma segunda despesa.** A fatura inteira já está lançada como
conta a pagar; o frete por conhecimento é o MESMO dinheiro repartido por rota e destinatário. Use
para responder "quanto do frete foi para o Rio?" ou "qual rota concentra o custo"; **nunca** some
\`frete_rs\` com \`gasto_por_periodo\`, \`gasto_por_fornecedor\` ou \`demonstrativo_despesas\` —
seria contar a mesma despesa duas vezes.

O conteúdo cobre hoje apenas os CT-e recebidos em fatura agregada. Campo vazio significa **"ainda
não extraído"**, nunca "o documento não tem". Diga isso quando a resposta depender disso.
Para contar, use a coluna \`total_encontrado\` (a contagem real do filtro, antes do limite), nunca
o número de linhas devolvidas. A cobertura é parcial: só o que chegou por e-mail e ainda tinha o
PDF guardado.

**Trilha de auditoria** (\`auditoria_eventos\`, \`auditoria_resumo\`): registra QUEM alterou o QUÊ
em contas a pagar e fornecedores, com o valor antes e depois de cada campo.

🔴 **A trilha começa em 11/08/2026.** Antes disso o sistema guardava apenas o ÚLTIMO editor de
cada conta, e a penúltima alteração era sobrescrita. Não achar evento antigo NÃO significa que
nada mudou — significa que não havia registro. Diga isso explicitamente quando a pergunta cobrir
período anterior.

🔴 **\`ator_via = "servico"\` NÃO quer dizer "ninguém".** Quer dizer alteração feita por rotina
automática (a leitura de e-mails a cada 5 min, o batch diário de baixa/vencidos) ou edição sem
autor atribuível. No resumo por usuário essas aparecem como "(automacao / nao atribuivel)".
Nunca apresente isso como se um humano tivesse feito, nem como se a alteração não tivesse ocorrido.

Para perguntas de VOLUME ou RANKING ("quais usuários mais alteram", "qual campo mais muda") use
\`auditoria_resumo\`, não a lista de eventos: a lista é truncada e contá-la daria número errado.
Alteração que não muda nada de fato não gera evento, e mudanças de escrituração automática
(updated_at e afins) ficam fora do registro de propósito.

**Pontualidade de pagamento** (\`pontualidade_pagamento\`) responde "pagamos em dia?", "qual o
atraso médio?" e "onde o atraso se concentra?".

🔴 **NÃO é DPO e não pode ser apresentada como tal.** DPO é indicador contábil (saldo de contas a
pagar ÷ CMV × dias) e exige o passivo da empresa; aqui a base é apenas o que chegou por e-mail.
Se perguntarem por DPO ou por "prazo médio de pagamento a fornecedores", responda o que existe —
pontualidade — e diga com franqueza que o DPO contábil não é calculável com estes dados.

🔴 **A cobertura é PARCIAL e precisa ser dita quando \`fora_da_cobertura\` for maior que zero.**
Só entram contas pagas a partir de \`cobertura_desde\`: antes disso a data de pagamento veio de um
preenchimento retroativo que a igualou ao vencimento, e incluí-la produziria "atraso zero" falso.
\`excluidas_venc_alterado\` são as contas cujo vencimento mudou DEPOIS do pagamento — ali o atraso
mediria a alteração do vencimento, não o pagamento.

🔴 **Em group_by="mes" vale a mesma regra do balde parcial de \`gasto_por_periodo\`**, e aqui ela
morde mais: o rótulo "2026-07" se lê como julho inteiro, mas o mês do corte tem poucos dias de
dado. Cada linha traz \`mes_parcial\` com \`dias_cobertos\` de \`dias_totais\` — nunca compare
\`contas\` ou \`valor_total\` de um mês parcial com os de um mês cheio, e diga quantos dias o mês
cobre. Nos demais eixos essas colunas vêm vazias, porque ali o grupo não delimita período.

\`pct_pontualidade\` conta como pontual o pagamento feito ATÉ o vencimento (antecipado inclusive).
\`atraso_medio_dias\` e \`atraso_mediano_dias\` somam **apenas as atrasadas** e vêm vazios quando
não houve nenhuma — vazio ali significa "não houve atraso", nunca "o atraso foi zero";
\`desvio_medio_dias\` é o líquido com sinal, em que antecipação abate atraso. Em
group_by="fornecedor", use \`min_contas\` e cite o número de contas: pontualidade sobre 1 conta é
ruído, não indicador.

**Origem da extração** (extraction_source): pdf_text vem do texto nativo do PDF (confiável);
pdf_vision e image_vision vêm de leitura visual (OCR) e podem conter erro de leitura; email_body
foi extraído do corpo do e-mail. Quando um número específico vier de OCR e a pergunta depender
dele, vale mencionar a origem.

## Visibilidade

Você enxerga exatamente as contas que este usuário já veria pela tela — nem mais, nem menos.
Se um resultado vier vazio, pode ser filtro sem correspondência OU ausência de permissão; não
especule sobre dados que não vieram.

## O que as ferramentas NÃO conseguem cruzar

Estas duas lacunas foram medidas em uso real. Reconhecê-las de imediato vale mais do que tentar
contorná-las: a tentativa queima o limite de consultas e ainda entrega resposta incompleta.

**Não existe cruzamento entre FORNECEDOR e CLASSIFICAÇÃO CONTÁBIL.** Perguntas como "quanto
gastei com fornecedores de tecnologia" ou "quais fornecedores são de manutenção" não têm
ferramenta. NÃO tente resolver consultando fornecedores um a um — já aconteceu de esgotar o
limite de consultas assim, sem chegar à resposta. Faça \`gasto_por_classificacao\` pela
categoria, apresente o total dela e diga com franqueza que a divisão por fornecedor dentro da
categoria não está disponível.

**Não existe eixo de empresa em \`gasto_por_periodo\`** — o filtro é uma empresa por vez. Para
comparar as três, chame a ferramenta uma vez por empresa (no máximo três) e monte a comparação
na resposta.

🔴 **Balde INCOMPLETO na série de \`gasto_por_periodo\`.** Cada linha traz \`is_partial\`, com
\`days_covered\` de \`days_total\` dias. O primeiro balde costuma começar antes do período pedido e
o último costuma terminar no FUTURO — dias que ainda não aconteceram. Comparar um balde parcial
com um completo é a leitura errada mais fácil de cometer: uma semana com 4 dias ao lado de uma
com 7 parece queda de 50% e não é. Sempre que citar um balde com \`is_partial\`, diga quantos dias
ele cobre; numa comparação, ou compare janelas de mesmo tamanho, ou avise que o período está em
curso. Vale igual para o mês corrente. Em date_field="vencimento" o futuro não torna o balde
parcial — ali são contas a vencer, que já existem na base.

## Exemplos de raciocínio

*"Quais os 5 maiores fornecedores com contas vencidas?"* → \`aging_vencidos\` com
group_by="fornecedor". Não use \`gasto_por_fornecedor\`: ela não filtra situação e devolveria os
maiores em GASTO, vencidos ou não — resposta plausível e errada.

*"Compare os gastos entre OTIMOTEX TECIDOS, LEBIANCO e OTIMOTEX FARDOS"* → três chamadas a
\`gasto_por_periodo\`, uma por sk_company (1, 2 e 3), no mesmo período.

*"Quanto gastamos de frete para o Rio de Janeiro?"* → \`documentos_fiscais\` com tipo=["cte"] e
rota="RIO", somando \`frete_rs\`. Diga que é a repartição do frete JÁ lançado como conta a pagar
e que cobre apenas os conhecimentos recebidos em fatura agregada — nunca some esse valor com
\`gasto_por_periodo\`.

*"Quanto gastei com fornecedores de tecnologia?"* → não há como cruzar fornecedor com
classificação. Responda pelo total da classificação (\`gasto_por_classificacao\`) e declare a
limitação, em vez de varrer fornecedores.

*"Como foi a semana passada comparada com esta?"* → \`gasto_por_periodo\` com
granularity="semana". A semana corrente virá com \`is_partial\` verdadeiro: informe o número
**com** os dias que ele cobre e não conclua queda a partir dele.

*"Estamos pagando em dia?"* → \`pontualidade_pagamento\` com group_by="geral" para o número, e
group_by="faixa" quando a pergunta for onde o atraso se concentra. Informe a cobertura se
\`fora_da_cobertura\` for maior que zero.

*"Qual o nosso DPO?"* → não é calculável: DPO exige o passivo contábil e o CMV da empresa, e aqui
só existe o que chegou por e-mail. Responda com \`pontualidade_pagamento\` (group_by="geral"),
deixando claro que é pontualidade de pagamento, não DPO.`;

/**
 * Teto de caracteres de UM resultado devolvido ao modelo.
 *
 * `listar_contas` com `limit=100` e as agregações por fornecedor produzem dezenas de KB, e cada
 * iteração reenvia TODO o histórico — sem teto, uma pergunta com 3 consultas grandes infla o
 * contexto quadraticamente e o custo junto.
 */
const MAX_TOOL_RESULT_CHARS = 60_000;

/** Converte o resultado da tool em texto para o modelo, com teto de tamanho. */
function toolResultText(rows: unknown[]): string {
  if (rows.length === 0) return 'Nenhum registro encontrado para este filtro.';

  const full = JSON.stringify(rows);
  if (full.length <= MAX_TOOL_RESULT_CHARS) return full;

  // Corta por REGISTRO, nunca no meio do JSON: uma string cortada ao meio é ilegível para o
  // modelo, que tentaria interpretar o fragmento como dado.
  let kept = rows.length;
  let text = full;
  while (kept > 1 && text.length > MAX_TOOL_RESULT_CHARS) {
    kept = Math.floor(kept / 2);
    text = JSON.stringify(rows.slice(0, kept));
  }

  // UM único registro pode estourar o teto sozinho (`additional_info` é TEXT sem limite no banco).
  // Aí não existe corte "por registro": ou se devolve o registro inteiro — furando justamente o
  // teto que protege o contexto — ou se corta a string. Cortar é o certo, desde que se DIGA que o
  // JSON ficou partido; caso contrário o modelo lê o fragmento final como se fosse dado.
  if (text.length > MAX_TOOL_RESULT_CHARS) {
    return `${text.slice(0, MAX_TOOL_RESULT_CHARS)}\n\n[JSON CORTADO no meio de um registro grande `
      + 'demais. Peça um recorte menor ou use um agregado em vez do detalhe.]';
  }

  return `${text}\n\n[Resultado truncado: ${kept} de ${rows.length} registros. Refine o filtro `
    + 'ou use um período menor para ver o restante.]';
}

/**
 * Roda o loop de tool use até o modelo parar de pedir ferramentas.
 *
 * @param supabase Cliente ANON (nunca service_role) — é o que faz a RLS valer.
 * @param token    JWT do usuário, repassado a cada RPC.
 * @param signal   Aborta o run quando o cliente desiste (`request.signal` da rota). Sem ele o
 *   loop segue gastando tokens numa resposta que ninguém vai receber — ver o comentário de
 *   `throwIfAborted`.
 * @param events   Observação opcional do progresso (rota SSE). Omitido, o turno se comporta
 *   exatamente como antes — ver `ChatProgress`.
 */
export async function runChat(
  supabase: SupabaseClient,
  token: string,
  req: ChatRequest,
  signal?: AbortSignal,
  events?: ChatProgress,
): Promise<ChatResult> {
  const anthropic = getAnthropic();
  const toolCalls: LoggedToolCall[] = [];
  // Acumulador único: com dois pontos de soma (loop e fechamento), somar campo a campo em cada um
  // é o tipo de duplicação que passa a divergir na primeira alteração.
  const usage = { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheCreationTokens: 0 };
  /**
   * Modelo que a API disse ter usado na última resposta. `null` enquanto nenhuma chegou.
   *
   * Guardado aqui, e não derivado no fim, porque é a ÚNICA janela em que ele existe: o objeto de
   * resposta é descartado a cada iteração.
   */
  let servedModel: string | null = null;
  /**
   * Recebe a MENSAGEM inteira, não só o `usage`, pelo mesmo motivo que o acumulador é único: são
   * dois pontos de chamada (o loop e o fechamento), e tudo que precise ser extraído de uma resposta
   * do modelo tem de sair daqui. Passando só o `usage`, registrar o modelo viraria uma segunda linha
   * a lembrar em cada call site — e o fechamento é exatamente o que alguém esqueceria.
   */
  const accumulate = (m: Anthropic.Message) => {
    usage.inputTokens += m.usage.input_tokens;
    usage.outputTokens += m.usage.output_tokens;
    usage.cacheReadTokens += m.usage.cache_read_input_tokens ?? 0;
    usage.cacheCreationTokens += m.usage.cache_creation_input_tokens ?? 0;
    if (m.model) servedModel = m.model;
  };
  let rowCount = 0;

  /**
   * Único ponto de montagem do `ChatResult`. Os DOIS caminhos de saída — resposta dentro do loop e
   * chamada de fechamento — montavam o objeto cada um por si; com a verificação de saúde do cache
   * isso viraria duas cópias da mesma checagem, e bastaria acrescentar um terceiro retorno para ela
   * passar a valer só para dois deles. Mesmo motivo do acumulador único de `usage`.
   */
  const finish = (texto: string, foiTruncado: boolean): ChatResult => {
    warnIfCachingDisabled(usage);
    return {
      answer: texto || SEM_RESPOSTA,
      toolCalls,
      rowCount,
      ...usage,
      truncated: foiTruncado,
      iterations,
      model: servedModel ?? CONFIGURED_MODEL,
    };
  };

  /**
   * Chama o modelo e devolve a mensagem final, repassando o texto em tempo real a quem observa.
   *
   * Existe para que o wiring do `on('text')` não seja copiado nas DUAS chamadas ao modelo — o loop
   * e o fechamento por teto de iterações. Duplicado, o fechamento seria justamente o caminho que
   * alguém esqueceria de manter, e ele é a resposta da pergunta MAIS CARA (a que estourou o teto):
   * o usuário veria a tela parada exatamente no turno mais longo, que é quando o streaming importa.
   *
   * O tipo do parâmetro é derivado do próprio SDK (`Parameters<...>`) em vez de escrito à mão:
   * assim uma mudança de assinatura numa atualização vira erro de compilação aqui, e não um `any`
   * silencioso.
   */
  const chamarModelo = (
    params: Parameters<typeof anthropic.messages.stream>[0],
  ): Promise<Anthropic.Message> => {
    const s = anthropic.messages.stream(params, { signal });
    if (events) {
      let emitiu = false;
      s.on('text', (delta: string) => {
        // O primeiro delta desta mensagem abre um bloco novo — ver `ChatProgress.onTextStart`.
        if (!emitiu) {
          emitiu = true;
          safeEmit(() => events.onTextStart?.(), 'text_start');
        }
        safeEmit(() => events.onTextDelta?.(delta), 'delta');
      });
    }
    return s.finalMessage();
  };

  // A data corrente vai AQUI (na mensagem), não no system prompt — ver SYSTEM_PROMPT.
  const hoje = new Date().toISOString().slice(0, 10);

  const messages: Anthropic.MessageParam[] = [
    ...(req.history ?? []).map((m) => ({ role: m.role, content: m.content })),
    { role: 'user' as const, content: `[Data de hoje: ${hoje}]\n\n${req.question}` },
  ];

  let truncated = false;
  /**
   * Chamadas ao modelo concluídas no loop. É o MESMO contador do teto e da auditoria: manter um
   * segundo, só para o log, é a duplicação que passa a divergir na primeira alteração. Conta as
   * concluídas (depois do `accumulate`), porque é para elas que existe usage medido.
   */
  let iterations = 0;

  try {
    for (;;) {
      // Checagem no LIMITE de cada iteração: é aqui que o run custa dinheiro. Um usuário que
      // clicou em "Parar" (ou fechou a aba) na 1ª de 3 iterações economiza as outras duas — sem
      // isto, o cliente desconecta e o servidor segue conversando com o modelo até o fim, cobrando
      // por uma resposta que ninguém vai ler.
      throwIfAborted(signal);

      if (iterations >= MAX_ITERATIONS) {
        truncated = true;
        break;
      }

      // `.stream()` + `.finalMessage()`, não `.create()` (§17.8): a resposta que chega ao cliente
      // é a mesma (JSON único), mas o socket recebe tokens continuamente em vez de ficar ocioso —
      // é o que evita timeout de request/proxy num turno longo, e é o ponto de onde a Fase 3
      // puxará o texto parcial para a tela.
      // O signal vai à chamada EM VOO dentro de `chamarModelo`: sem ele o abort só seria notado na
      // próxima iteração, e um turno longo continuaria streamando até terminar.
      const response = await chamarModelo({
        model: CONFIGURED_MODEL,
        max_tokens: MAX_TOKENS,
        // cache_control no bloco estável: só o system+tools é cacheado; a pergunta (volátil)
        // vem depois e não invalida o prefixo.
        system: [{ type: 'text', text: SYSTEM_PROMPT, cache_control: { type: 'ephemeral' } }],
        tools: toolParams,
        messages,
      });

      accumulate(response);
      iterations += 1;

      // Preserva a resposta INTEIRA (inclui os blocos tool_use) — extrair só o texto quebraria
      // o pareamento tool_use/tool_result na próxima iteração.
      messages.push({ role: 'assistant', content: response.content });

      if (response.stop_reason !== 'tool_use') {
        // `max_tokens` também cai aqui: o turno terminou, mas o texto foi CORTADO no meio. Tratá-lo
        // como resposta completa entregaria uma frase pela metade sem nenhum sinal ao usuário.
        return finish(extractText(response.content), response.stop_reason === 'max_tokens');
      }

      // Tool calls PARALELOS voltam em UMA única mensagem user (§17.6). Dividi-los em mensagens
      // separadas ensina o modelo a parar de paralelizar.
      const results: Anthropic.ToolResultBlockParam[] = [];

      for (const block of response.content) {
        if (block.type !== 'tool_use') continue;
        const started = Date.now();

        if (!isToolName(block.name)) {
          results.push(errorResult(block.id, `Ferramenta desconhecida: ${block.name}`));
          toolCalls.push({ name: block.name, params: {}, rows: 0, ms: 0, error: 'desconhecida' });
          continue;
        }

        const parsed = parseToolInput(block.name, block.input);
        if (!parsed.ok) {
          results.push(errorResult(block.id, parsed.message));
          toolCalls.push({
            name: block.name, params: {}, rows: 0,
            ms: Date.now() - started, error: parsed.message,
          });
          continue;
        }

        // Só as chamadas VÁLIDAS viram progresso na tela. Os dois desvios acima (tool inexistente,
        // parâmetro fora do schema) seguem no log e no `toolCalls`, mas não geram chip: são defeito
        // nosso ou do modelo, não trabalho que o usuário pediu, e anunciá-los transformaria a barra
        // de progresso num relatório de erro interno.
        safeEmit(() => events?.onToolStart?.({ name: block.name, params: parsed.params }), 'tool');

        try {
          const rows = await runTool(supabase, token, block.name, parsed.params);
          rowCount += rows.length;
          results.push({ type: 'tool_result', tool_use_id: block.id, content: toolResultText(rows) });
          toolCalls.push({
            name: block.name, params: parsed.params,
            rows: rows.length, ms: Date.now() - started,
          });
          safeEmit(
            () => events?.onToolEnd?.({
              name: block.name, rows: rows.length, ms: Date.now() - started,
            }),
            'tool_end',
          );
        } catch (e) {
          // Falha da tool volta ao modelo como tool_result com is_error (§17.6) — nunca omitir o
          // bloco, que quebraria o pareamento. O detalhe fica no log, não vai ao modelo.
          const detalhe = e instanceof Error ? e.message : String(e);
          console.error(`[ai-chat] tool ${block.name} falhou:`, detalhe);
          results.push(errorResult(block.id, 'A consulta falhou. Tente outra abordagem.'));
          toolCalls.push({
            name: block.name, params: parsed.params, rows: 0,
            ms: Date.now() - started, error: detalhe,
          });
          // O chip fecha com marca de falha. `detalhe` NÃO vai junto — é a mesma regra que já vale
          // para o modelo e para a resposta HTTP: mensagem interna não chega ao cliente.
          safeEmit(
            () => events?.onToolEnd?.({
              name: block.name, rows: 0, ms: Date.now() - started, error: 'falhou',
            }),
            'tool_end',
          );
        }
      }

      messages.push({ role: 'user', content: results });
    }

    // Saiu por MAX_ITERATIONS: pede uma resposta final SEM tools, para o usuário receber o que já
    // foi apurado em vez de um erro seco. Fica DENTRO do try — um 429 nesta chamada precisa da
    // mesma tradução das demais, senão vira 500 genérico justamente na pergunta mais cara.
    const fechamento = await chamarModelo({
      model: CONFIGURED_MODEL,
      max_tokens: MAX_TOKENS,
      system: [{ type: 'text', text: SYSTEM_PROMPT, cache_control: { type: 'ephemeral' } }],
      // As tools vão JUNTO, com `tool_choice: none` — não basta omiti-las (não regredir).
      // Remover o array `tools` é uma mudança de DEFINIÇÃO de tool, que invalida os três níveis
      // de cache (tools + system + messages): o fechamento é a chamada com o histórico mais
      // longo, e pagaria prefixo cheio justamente aí. Trocar só o `tool_choice` preserva o
      // cache. De quebra, o histórico contém blocos `tool_use`, que a API espera acompanhados
      // da definição das tools.
      tools: toolParams,
      tool_choice: { type: 'none' },
      messages: [
        ...messages,
        {
          role: 'user',
          content:
            'Você atingiu o limite de consultas para esta pergunta. Responda agora com o que já '
            + 'apurou, deixando claro o que ficou sem verificar.',
        },
      ],
    });
    accumulate(fechamento);

    return finish(extractText(fechamento.content), truncated);
  } catch (e) {
    // Anexa o que JÁ foi gasto e apurado. Sem isso, uma falha na 5ª iteração é auditada como
    // "0 tokens, 0 tools" — some justamente o registro das perguntas caras que falharam, que é a
    // fonte para descobrir quais tools faltam (§11) e para não subestimar o custo real.
    //
    // Quem decide se foi aborto é o SIGNAL, não o tipo do erro — ver o comentário de
    // `AiChatAbortedError`. Se o cliente já desistiu, o que o SDK levantou é consequência disso, e
    // traduzir viraria 500 genérico ("erro interno"), auditando um cancelamento como falha do
    // assistente e poluindo a fonte que serve para achar o que está realmente quebrado. O estado
    // parcial vai junto: cancelar não devolve os tokens já gastos, e a auditoria de custo os quer.
    const erro = signal?.aborted ? new AiChatAbortedError() : translateAnthropicError(e);
    throw attachPartialRun(erro, {
      ...usage, toolCalls, rowCount, iterations,
      model: servedModel ?? CONFIGURED_MODEL,
    });
  }
}

/**
 * Levanta `AiChatAbortedError` se o cliente já desistiu.
 *
 * NÃO propaga o signal para as RPCs de `runTool` — decisão deliberada: uma consulta abortada
 * apareceria dentro do `catch` por tool, que por contrato converte falha em `tool_result` com
 * `is_error` e SEGUE o loop (§17.6). Distinguir abort ali criaria um segundo caminho de
 * cancelamento, meio-tratado, para uma chamada que leva milissegundos — enquanto esta checagem, no
 * limite da iteração, pega o mesmo abort imediatamente depois e para o loop de verdade.
 */
function throwIfAborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) throw new AiChatAbortedError();
}

/**
 * Detecta EM RUNTIME o modo de falha descrito no comentário de `CONFIGURED_MODEL`: prefixo abaixo do mínimo do
 * modelo faz a API ignorar o `cache_control` **sem erro nenhum** — o turno responde normalmente e
 * só a fatura acusa.
 *
 * Por que em código, e não como número na documentação: o prefixo não é constante — ele cresce a
 * cada tool acrescentada, e entre 30/07 e 10/08/2026 dobrou (3.653 → 7.408), invertendo a
 * conclusão sobre o risco. Documentar um valor exige que alguém se lembre de re-medi-lo depois de
 * cada tool nova e de cada troca de modelo; esta checagem roda sozinha, em todo turno, e não tem
 * como ficar desatualizada.
 *
 * `cacheRead === 0 && cacheCreation === 0` é sinal SEGURO, não heurística: com `cache_control` no
 * bloco estável, um turno saudável ou **cria** o prefixo (1ª chamada, ou TTL expirado) ou o **lê**
 * (chamadas seguintes). Zerar os dois só acontece se a API ignorou o `cache_control`. Não há
 * guarda para "nenhuma chamada feita" porque não existe caminho até aqui sem ao menos um
 * `accumulate`: o teto de iterações é 6, então o laço roda no mínimo uma vez.
 *
 * É aviso, não erro: o usuário recebeu a resposta certa. O que está errado é o CUSTO, e quem age
 * sobre isso lê o log da plataforma — por isso `console.error` (o canal que a Vercel coleta) em vez
 * de lançar.
 */
function warnIfCachingDisabled(u: { cacheReadTokens: number; cacheCreationTokens: number }): void {
  if (u.cacheReadTokens > 0 || u.cacheCreationTokens > 0) return;
  console.error(
    `[ai-chat] prompt caching NÃO ocorreu (modelo ${CONFIGURED_MODEL}): cache_read e cache_creation zerados. `
    + 'O prefixo system+tools provavelmente está abaixo do mínimo deste modelo — o cache_control é '
    + 'ignorado sem erro e o custo por pergunta sobe. Confira ANTHROPIC_MODEL.',
  );
}

/**
 * Executa um callback de progresso sem deixar que ele derrube o turno.
 *
 * 🔴 Não é zelo excessivo: quem observa é a rota SSE, e `controller.enqueue` **lança** quando o
 * cliente já fechou a conexão — o caso mais comum de todos, porque é exatamente o que acontece
 * quando o usuário clica em "Parar" ou fecha a aba. Sem esta contenção, uma desconexão normal
 * viraria exceção no meio do loop, abortaria o turno por um motivo que não é erro e — pior —
 * pularia a auditoria, que é o único registro do que já foi gasto.
 *
 * O turno continua até o fim mesmo sem ninguém escutando: quem decide parar por desistência do
 * cliente é o `throwIfAborted`, no limite da iteração, que é o ponto onde parar é seguro.
 */
function safeEmit(fn: () => void, rotulo: string): void {
  try {
    fn();
  } catch (e) {
    console.error(`[ai-chat] callback de progresso (${rotulo}) falhou:`, e);
  }
}

function errorResult(id: string, message: string): Anthropic.ToolResultBlockParam {
  return { type: 'tool_result', tool_use_id: id, content: message, is_error: true };
}

function extractText(content: Anthropic.ContentBlock[]): string {
  return content
    .filter((b): b is Anthropic.TextBlock => b.type === 'text')
    .map((b) => b.text)
    .join('\n')
    .trim();
}
