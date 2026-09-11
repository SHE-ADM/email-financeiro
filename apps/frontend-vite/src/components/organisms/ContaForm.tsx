// src/components/organisms/ContaForm.tsx
// Organism — formulário de conta a pagar (criação rápida / edição). Estado via
// react-hook-form; validação no submit com financialAccountControlCreateSchema
// (@sheild/shared). Fornecedor via react-select (SupplierSelect). Tipo de documento
// e tipo de pagamento são selects de APENAS CONSULTA (valores pré-definidos dos enums,
// obrigatórios). O envio (POST/PATCH na Next API) é responsabilidade do pai.
import { forwardRef, useImperativeHandle, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import {
  financialAccountControlCreateSchema,
  DOCUMENT_TYPES,
  PAYMENT_METHODS,
  type FinancialAccountAttachment,
  type FinancialAccountControl,
  type FinancialAccountControlCreate,
} from '@sheild/shared';
import AuthInput from '../atoms/AuthInput';
import Alert from '../atoms/Alert';
import LabeledSelect, { type SelectOption } from '../atoms/LabeledSelect';
import SupplierSelect from '../molecules/SupplierSelect';
import CostCenterSelect from '../molecules/CostCenterSelect';
import ChartAccountSelect from '../molecules/ChartAccountSelect';
import AttachmentPicker from '../molecules/AttachmentPicker';
import ContaAttachments from './ContaAttachments';
import { todayISO } from '../../lib/format';
import { getSupplier } from '../../services/suppliers';
import { useCompanyOptions } from '../../hooks/useCompanyOptions';
import { useDefaultSkCompany, SK_COMPANY_DEFAULT } from '../../hooks/useDefaultSkCompany';

// Opções dos selects de enum ordenadas alfabeticamente (pt-BR) — os valores são os
// mesmos dos CHECK do banco; só a ordem de exibição muda.
const DOCUMENT_TYPE_OPTIONS = [...DOCUMENT_TYPES].sort((a, b) => a.localeCompare(b, 'pt-BR'));
const PAYMENT_METHOD_OPTIONS = [...PAYMENT_METHODS].sort((a, b) => a.localeCompare(b, 'pt-BR'));

// Empresa pagadora (financial_account_control.sk_company) — 1 OTIMOTEX TECIDOS (default),
// 2 LEBIANCO, 3 OTIMOTEX FARDOS, 4 LE BLANC. Na CRIAÇÃO o inicial vem de useDefaultSkCompany (a ester
// nasce em FARDOS; os demais, em TECIDOS); na edição, da própria conta. Na extração quem
// define é a regra de precedência do read_emails.py, não este form.
// Fallback só para quando o lookup falhar — o select nunca fica vazio e o lançamento não trava.
const COMPANY_FALLBACK_OPTION: SelectOption = { value: SK_COMPANY_DEFAULT, label: 'OTIMOTEX TECIDOS' };

interface ContaFormValues {
  amount: string;
  document_type: string;
  payment_method: string;
  due_date: string;
  issue_date: string;
  invoice_number: string;
  description: string;
  barcode: string;
  additional_info: string;
}

interface ContaFormProps {
  mode: 'create' | 'edit';
  defaultValues?: FinancialAccountControl;
  /**
   * Recebe também a FILA de anexos escolhidos, que o pai envia DEPOIS de gravar a conta
   * (o upload precisa do id, que na inclusão só existe após o POST). O form não sobe
   * arquivo: mantê-lo declarativo é o que permite testar submit e fila juntos.
   *
   * @returns os arquivos que devem PERMANECER na fila — normalmente os que NÃO subiram.
   *   `undefined`/`void` esvazia a fila (sucesso). É isto que impede o 2º submit de
   *   reenviar o que já subiu e **DUPLICAR** o anexo: cada upload gera uma `storage_key`
   *   nova (timestamp + aleatório), então o UNIQUE do banco não deduplicaria. Importa no
   *   modal de edição, que NÃO remonta e fica aberto quando um upload falha.
   *
   *   O pai reporta os próprios erros por `submitError` (padrão do projeto) e devolve a
   *   fila para não perder os arquivos escolhidos. Se ainda assim lançar, a fila fica
   *   intacta — o `setFiles` só roda depois do `await`.
   */
  onSubmit: (data: FinancialAccountControlCreate, pendingFiles: File[]) => Promise<File[] | void>;
  onCancel?: () => void;
  submitError?: string | null;
  submitting?: boolean;
  /** Anexos já salvos, para o pai refletir a remoção na sua cópia da conta (modo edit). */
  onAttachmentsChanged?: (attachments: FinancialAccountAttachment[]) => void;
}

/** Handle imperativo do ContaForm (uso: lançamento em série na página /contas). */
export interface ContaFormHandle {
  /**
   * Limpa APENAS o fornecedor e foca o seletor, preservando os demais campos — para
   * lançar contas em série. Não mexe na classificação (centro/plano) já digitada; ela
   * só é re-semeada quando o usuário ESCOLHE um novo fornecedor (handleSupplierChange).
   */
  resetSupplier: () => void;
}

function toFormValues(c?: FinancialAccountControl): ContaFormValues {
  // Inclusão (sem `c`): emissão e vencimento já vêm com a data de hoje. Edição:
  // mantém os valores da conta (sem fabricar data quando o campo está vazio).
  const dateDefault = c ? '' : todayISO();
  return {
    amount: c?.amount != null ? String(c.amount) : '',
    document_type: c?.document_type ?? '',
    payment_method: c?.payment_method ?? '',
    due_date: c?.due_date ?? dateDefault,
    issue_date: c?.issue_date ?? dateDefault,
    invoice_number: c?.invoice_number ?? '',
    description: c?.description ?? '',
    barcode: c?.barcode ?? '',
    additional_info: c?.additional_info ?? '',
  };
}

const blankToNull = (v: string): string | null => (v.trim() ? v.trim() : null);

// id 0 = "não informado" (sentinela do banco) → tratado como vazio na UI.
const orNull = (id: number | null | undefined): number | null => (id ? id : null);

// Fonte estrutural de rótulo da classificação — atende tanto a conta (modo edição)
// quanto o fornecedor (pré-preenchimento), ambos com cost_center_id/chart_account_id
// + embeds dos cadastros.
type ClassificationSource = Pick<
  FinancialAccountControl,
  'cost_center_id' | 'chart_account_id' | 'cost_center' | 'chart_account'
>;

// Rótulo do centro de custo já vinculado — usa o embed do JOIN; undefined quando não
// há classificação (id 0/ausente).
const costCenterDefaultLabel = (c?: ClassificationSource): string | undefined => {
  if (!c?.cost_center_id) return undefined;
  return c.cost_center?.cost_center_description ?? c.cost_center?.cost_center_code ?? `#${c.cost_center_id}`;
};

// Descrição do plano de contas já vinculado — é o VALOR do 1º select (cascata invertida
// Plano → Centro). Vem do embed do JOIN; null quando não há classificação (id 0/ausente).
const chartAccountDescriptionOf = (c?: ClassificationSource): string | null => {
  if (!c?.chart_account_id) return null;
  return c.chart_account?.account_description ?? null;
};

const ContaForm = forwardRef<ContaFormHandle, ContaFormProps>(function ContaForm({
  mode,
  defaultValues,
  onSubmit,
  onCancel,
  submitError,
  submitting = false,
  onAttachmentsChanged,
}, ref) {
  const {
    register,
    handleSubmit,
    setError,
    formState: { errors },
  } = useForm<ContaFormValues>({ defaultValues: toFormValues(defaultValues) });

  const [skSupplier, setSkSupplier] = useState<number | null>(defaultValues?.sk_supplier ?? null);
  const [supplierError, setSupplierError] = useState<string | null>(null);
  // Empresa PAGADORA — FK sk_company. Criação nasce no default POR USUÁRIO (ester → FARDOS;
  // demais → TECIDOS); edição mostra a empresa da própria conta. Fora do react-hook-form como
  // os demais FKs (skSupplier/costCenterId), pois ContaFormValues é só de strings. NÃO é limpo
  // pelo resetSupplier: no lançamento EM SÉRIE a empresa escolhida PERMANECE (decisão do
  // usuário), igual aos selects de classificação — só o fornecedor é limpo.
  const defaultSkCompany = useDefaultSkCompany();
  const [skCompany, setSkCompany] = useState<number>(defaultValues?.sk_company ?? defaultSkCompany);
  // `key` do SupplierSelect: incrementar REMONTA só o seletor de fornecedor (limpa o texto
  // interno do react-select, que não espelha `value`) e dispara o autofoco (key > 0). NÃO
  // afeta os selects de classificação (centro/plano permanecem — ver [[conta-form-...]]).
  const [supplierKey, setSupplierKey] = useState(0);
  // Classificação contábil (opcional) — cascata INVERTIDA Plano → Centro. O 1º select
  // guarda a DESCRIÇÃO do plano; o 2º (centro) resolve o chart_account_id + o cost_center_id.
  // id 0 (sentinela "não informado") aparece vazio nos selects.
  const [chartAccountDescription, setChartAccountDescription] = useState<string | null>(
    chartAccountDescriptionOf(defaultValues),
  );
  const [costCenterId, setCostCenterId] = useState<number | null>(orNull(defaultValues?.cost_center_id));
  const [chartAccountId, setChartAccountId] = useState<number | null>(orNull(defaultValues?.chart_account_id));
  // Erro do par de classificação (plano sem centro) — espelha a trava da Next API na UI.
  const [classificationError, setClassificationError] = useState<string | null>(null);
  // Classificação DEFAULT do fornecedor selecionado — fonte dos rótulos ao re-semear.
  // Fica preenchido depois que o usuário ESCOLHE/TROCA o fornecedor (create OU edição);
  // antes disso (edição), os rótulos vêm de `defaultValues` (a própria conta). Os selects
  // de centro/plano são CONTROLADOS (espelham `value`), então refletem o novo valor/rótulo
  // sozinhos ao atualizar costCenterId/chartAccountId — sem remonte por `key`.
  const [prefill, setPrefill] = useState<ClassificationSource | null>(null);
  // Fila de anexos a enviar: fica em MEMÓRIA e sobe só depois que o pai gravar a conta
  // (na inclusão o id só existe após o POST; no modo edição, subir ao escolher deixaria o
  // arquivo órfão se o usuário cancelasse o modal).
  const [files, setFiles] = useState<File[]>([]);
  // Descarta respostas obsoletas de getSupplier quando o fornecedor é trocado em sequência.
  const supplierReqRef = useRef(0);

  // Empresas do <select> (mesmo hook do filtro de /consulta). Falha na rede NÃO trava o
  // lançamento: sem opções, cai no fallback OTIMOTEX — o default e o caso da maioria.
  const loadedCompanies = useCompanyOptions();
  const companyOptions = loadedCompanies.length ? loadedCompanies : [COMPANY_FALLBACK_OPTION];

  // Limpa APENAS o fornecedor e foca o seletor (o pai chama após lançar uma conta, para o
  // próximo lançamento em série). Não navega, não remonta o form nem mexe nos demais campos.
  useImperativeHandle(ref, () => ({
    resetSupplier() {
      setSkSupplier(null);
      setSupplierError(null);
      setSupplierKey((k) => k + 1);
    },
  }), []);

  // Cascata INVERTIDA: trocar (ou limpar) o PLANO zera o centro, que pode não pertencer ao
  // novo plano. O CostCenterSelect remonta via `key={chartAccountDescription}`.
  const handlePlanoChange = (description: string | null) => {
    setChartAccountDescription(description);
    setChartAccountId(null);
    setCostCenterId(null);
    setClassificationError(null);
  };

  // Escolher o CENTRO resolve o chart_account_id (a linha do plano naquele centro) e o
  // cost_center_id — gravados juntos, sempre consistentes.
  const handleCentroChange = (caId: number | null, ccId: number | null) => {
    setChartAccountId(caId);
    setCostCenterId(ccId);
    setClassificationError(null);
  };

  // Ao ESCOLHER/TROCAR o fornecedor (ação do usuário — o onChange do SupplierSelect NÃO
  // dispara no mount), re-semeia Centro de custo / Plano de contas com a classificação
  // default do supplier (migration 052). Vale nos DOIS modos: em EDIÇÃO corrige o caso do
  // fornecedor trocado (ex.: boleto securitizado reatribuído ao credor real) que antes
  // mantinha a classificação 0/0 do fornecedor anterior. Novo fornecedor sem default (0/0)
  // limpa a classificação (o usuário define manualmente). Limpar o fornecedor (null) não
  // mexe na classificação já digitada. Fetch-on-change com guarda anti-corrida por reqId.
  const handleSupplierChange = (newSk: number | null) => {
    setSkSupplier(newSk);
    const reqId = ++supplierReqRef.current;
    if (newSk == null) return;
    void (async () => {
      try {
        const sup = await getSupplier(newSk);
        if (supplierReqRef.current !== reqId) return; // resposta obsoleta — fornecedor mudou
        const cc = sup.cost_center_id || 0;
        const ca = sup.chart_account_id || 0;
        setPrefill(cc || ca ? sup : null);
        setChartAccountDescription(ca ? (sup.chart_account?.account_description ?? null) : null);
        setCostCenterId(cc || null);
        setChartAccountId(ca || null);
        setClassificationError(null);
      } catch {
        // Falha ao buscar defaults não bloqueia o lançamento — segue sem re-semear.
      }
    })();
  };

  const submit = handleSubmit(async (raw) => {
    setSupplierError(null);
    let ok = true;
    if (skSupplier == null) {
      setSupplierError('Selecione um fornecedor');
      ok = false;
    }
    if (!raw.document_type) {
      setError('document_type', { message: 'Selecione o tipo de documento' });
      ok = false;
    }
    if (!raw.payment_method) {
      setError('payment_method', { message: 'Selecione o tipo de pagamento' });
      ok = false;
    }
    // Par de classificação: plano informado exige o centro (espelha a trava da Next API).
    setClassificationError(null);
    if (chartAccountDescription && chartAccountId == null) {
      setClassificationError('Selecione o centro de custo do plano informado');
      ok = false;
    }

    const payload = {
      sk_supplier: skSupplier ?? undefined,
      sk_company: skCompany,
      // Não informado → 0 (sentinela). A coluna é NOT NULL DEFAULT 0 (migration 048).
      cost_center_id: costCenterId ?? 0,
      chart_account_id: chartAccountId ?? 0,
      amount: raw.amount ? Number(raw.amount.replace(',', '.')) : undefined,
      document_type: raw.document_type || null,
      payment_method: raw.payment_method || null,
      due_date: blankToNull(raw.due_date),
      issue_date: blankToNull(raw.issue_date),
      invoice_number: blankToNull(raw.invoice_number),
      description: blankToNull(raw.description),
      barcode: blankToNull(raw.barcode),
      additional_info: blankToNull(raw.additional_info),
    };

    const parsed = financialAccountControlCreateSchema.safeParse(payload);
    if (!parsed.success) {
      for (const issue of parsed.error.issues) {
        const field = issue.path[0];
        // Mensagem amigável para o fornecedor (o Zod diria "expected number, received undefined").
        if (field === 'sk_supplier') setSupplierError('Selecione um fornecedor');
        else if (typeof field === 'string' && field in raw) setError(field as keyof ContaFormValues, { message: issue.message });
      }
      ok = false;
    }

    if (!ok || !parsed.success) return;
    // A fila vai junto: quem grava a conta é o pai, e só ele tem o id necessário ao upload.
    // O retorno diz o que SOBROU (os que não subiram) — sem isso, um 2º submit no modal
    // (que fica aberto na falha parcial) reenviaria os já enviados e os duplicaria.
    // Exceção do onSubmit não chega aqui: a fila fica intacta para nova tentativa.
    const remaining = await onSubmit(parsed.data, files);
    setFiles(remaining ?? []);
  });

  // Fonte dos rótulos dos selects de classificação: depois que o usuário escolhe/troca o
  // fornecedor, o default dele (prefill); antes disso, a própria conta (edição) ou nada.
  const labelSource: ClassificationSource | undefined = prefill ?? defaultValues ?? undefined;

  return (
    <form onSubmit={submit} className="space-y-3" noValidate>
      {submitError && <Alert variant="error">{submitError}</Alert>}

      <SupplierSelect
        key={supplierKey}
        autoFocus={supplierKey > 0}
        id="conta-supplier"
        label="Fornecedor"
        value={skSupplier}
        defaultLabel={defaultValues?.supplier?.trade_name ?? defaultValues?.supplier?.legal_name}
        onChange={handleSupplierChange}
        error={supplierError ?? undefined}
      />

      {/* Empresa PAGADORA — logo após o Fornecedor. São coisas distintas: pode haver conta
          da LEBIANCO cujo fornecedor é a OTIMOTEX. Sem placeholder: sempre há um valor
          (nasce no default OTIMOTEX), então o campo nunca fica vazio. */}
      <LabeledSelect
        id="conta-company"
        label="Empresa"
        options={companyOptions}
        value={skCompany}
        onChange={(e) => setSkCompany(Number(e.target.value))}
      />

      <AuthInput label="Descrição" error={errors.description?.message} {...register('description')} />

      {/* Cascata INVERTIDA: Plano de contas PRIMEIRO; o centro reflete o plano escolhido. */}
      <ChartAccountSelect
        id="conta-chart-account"
        label="Plano de contas"
        value={chartAccountDescription}
        onChange={handlePlanoChange}
      />

      <CostCenterSelect
        id="conta-cost-center"
        label="Centro de custo"
        planoDescription={chartAccountDescription}
        value={chartAccountId}
        defaultLabel={costCenterDefaultLabel(labelSource)}
        onChange={handleCentroChange}
        error={classificationError ?? undefined}
      />

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="block text-sm font-medium text-gray-700 mb-1">Tipo de documento</span>
          <select
            aria-invalid={errors.document_type ? true : undefined}
            className="input"
            {...register('document_type')}
          >
            <option value="">Selecione…</option>
            {DOCUMENT_TYPE_OPTIONS.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
          {errors.document_type && <span className="block mt-1 text-xs text-status-error-fg">{errors.document_type.message}</span>}
        </label>

        <label className="block">
          <span className="block text-sm font-medium text-gray-700 mb-1">Tipo de pagamento</span>
          <select
            aria-invalid={errors.payment_method ? true : undefined}
            className="input"
            {...register('payment_method')}
          >
            <option value="">Selecione…</option>
            {PAYMENT_METHOD_OPTIONS.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
          {errors.payment_method && <span className="block mt-1 text-xs text-status-error-fg">{errors.payment_method.message}</span>}
        </label>

        <AuthInput label="Nº do documento" error={errors.invoice_number?.message} {...register('invoice_number')} />
        <AuthInput label="Emissão" type="date" error={errors.issue_date?.message} {...register('issue_date')} />

        <AuthInput
          label="Valor (R$)"
          type="number"
          step="0.01"
          min="0"
          placeholder="0,00"
          error={errors.amount?.message}
          {...register('amount')}
        />
        <AuthInput label="Vencimento" type="date" error={errors.due_date?.message} {...register('due_date')} />
      </div>

      <AuthInput label="Código de barras" error={errors.barcode?.message} {...register('barcode')} />

      <label className="block">
        <span className="block text-sm font-medium text-gray-700 mb-1">Informações adicionais</span>
        <textarea
          className="input min-h-16"
          rows={2}
          placeholder="Observações livres sobre a conta…"
          {...register('additional_info')}
        />
      </label>

      {/* Anexos já salvos — só na edição (na inclusão a conta ainda não existe). O de
          e-mail aparece com selo e sem lixeira; remover é imediato, com confirmação. */}
      {mode === 'edit' && defaultValues && (
        <ContaAttachments
          accountId={defaultValues.id}
          items={defaultValues.attachments}
          legacySourceFile={defaultValues.source_file}
          onChanged={onAttachmentsChanged}
          title="Anexos da conta"
        />
      )}

      {/* Fila de novos anexos — nos DOIS modos; sobe após o pai gravar a conta. */}
      <AttachmentPicker
        value={files}
        onChange={setFiles}
        disabled={submitting}
        label={mode === 'edit' ? 'Adicionar anexos' : 'Anexos'}
      />

      <div className="flex justify-end gap-2 pt-1">
        {onCancel && (
          <button type="button" onClick={onCancel} className="btn" disabled={submitting}>
            Cancelar
          </button>
        )}
        <button type="submit" className="btn btn-primary" disabled={submitting}>
          {submitting ? 'Salvando…' : mode === 'create' ? 'Lançar conta' : 'Salvar alterações'}
        </button>
      </div>
    </form>
  );
});

ContaForm.displayName = 'ContaForm';

export default ContaForm;
