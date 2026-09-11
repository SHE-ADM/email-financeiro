<#
.SYNOPSIS
  Atualiza o deploy de produção do Pagamentos (C:\Sheild\API\Pagamentos) a partir
  de um bundle com os arquivos novos. Com backup, ajuste seguro do .env e validação.

.DESCRIPTION
  Produção é um deploy MANUAL (não é clone git) e só roda os batches: cobrança
  (run.py) e email reader. Este script copia APENAS o que produção usa, faz backup
  do que sobrescreve, garante o throttle no .env e valida que o run.py importa
  (pega o erro clássico de esquecer o send_core.py — dependência NOVA do run.py).

  NÃO toca: server\ (Flask), apps\ (frontend), tests\, nem tarefas do Agendador.
  NÃO precisa "reiniciar" nada: a tarefa de horário inicia um processo novo às 10:00
  e lê os arquivos do disco naquele instante.

  Por padrão roda em PREVIEW (não copia). Use -Apply para efetivar.

.PARAMETER Source
  Raiz do bundle com os arquivos novos (mesma estrutura do repo: skills\..., .env,
  scheduler\...). Default: a pasta acima deste script (se você rodar de dentro do bundle).

.PARAMETER Dest
  Raiz do deploy de produção. Default: C:\Sheild\API\Pagamentos.

.PARAMETER IncludeReader
  Copia também o fix do email reader (read_emails.py + extract_pdf.py).

.PARAMETER IncludeScheduler
  Copia também os scripts de scheduler (restart-sheild-tasks.ps1 + este deploy-prod.ps1).

.PARAMETER Apply
  Efetiva as mudanças. Sem isto, só mostra o que faria (preview).

.PARAMETER DryRunAfter
  Após aplicar, roda `run.py --dry-run` (valida imports + conexão Firebird; NÃO envia).

.EXAMPLE
  .\deploy-prod.ps1
  # Preview: mostra o que copiaria e o estado do .env

.EXAMPLE
  .\deploy-prod.ps1 -Source C:\Temp\pag-deploy -Apply -DryRunAfter
  # Copia de C:\Temp\pag-deploy, faz backup, ajusta .env e valida com dry-run
#>

[CmdletBinding()]
param(
  [string]$Source = (Split-Path $PSScriptRoot -Parent),
  [string]$Dest   = 'C:\Sheild\API\Pagamentos',
  [switch]$IncludeReader,
  [switch]$IncludeScheduler,
  [switch]$Apply,
  [switch]$DryRunAfter
)

$ErrorActionPreference = 'Stop'

# ── Constantes ──────────────────────────────────────────────────────────────
$THROTTLE_KEY   = 'COBRANCA_SEND_DELAY_SECONDS'
$THROTTLE_VALUE = '10'
$COBRANCA_REL   = 'skills\cobranca-vencidos\scripts'
# Arquivos cuja AUSÊNCIA no source aborta o deploy (send_core é a dependência nova).
$REQUIRED_IN_SOURCE = @('run.py', 'send_core.py', 'supabase_log.py')
$READER_FILES = @(
  'skills\email-reader\scripts\read_emails.py',
  'skills\pdf-contas-pagar\scripts\extract_pdf.py'
)
$SCHEDULER_FILES = @(
  'scheduler\restart-sheild-tasks.ps1',
  'scheduler\deploy-prod.ps1'
)

function Resolve-RelFiles($root, $rel) {
  # Lista de pares (rel, srcFull) para todos os .py de uma pasta relativa.
  $dir = Join-Path $root $rel
  if (-not (Test-Path $dir)) { return @() }
  Get-ChildItem -Path $dir -Filter '*.py' -File | ForEach-Object {
    [pscustomobject]@{ Rel = (Join-Path $rel $_.Name); Src = $_.FullName }
  }
}

# ── 0. Sanidade ─────────────────────────────────────────────────────────────
if (-not (Test-Path $Source)) { throw "Source não existe: $Source" }
if (-not (Test-Path $Dest))   { throw "Dest (produção) não existe: $Dest. Esta é a máquina de produção?" }
$Source = (Resolve-Path $Source).Path
$Dest   = (Resolve-Path $Dest).Path
Write-Output "Source : $Source"
Write-Output "Dest   : $Dest"
Write-Output ("Modo   : " + $(if ($Apply) { 'APLICAR' } else { 'PREVIEW (use -Apply para efetivar)' }))
Write-Output ""

# ── 1. Monta a lista de arquivos a copiar ───────────────────────────────────
$plan = @()
$plan += Resolve-RelFiles $Source $COBRANCA_REL      # cobrança: sempre (pasta inteira)
if ($IncludeReader) {
  foreach ($r in $READER_FILES) {
    $src = Join-Path $Source $r
    if (Test-Path $src) { $plan += [pscustomobject]@{ Rel = $r; Src = $src } }
    else { Write-Warning "Reader: não achei no source, pulando: $r" }
  }
}
if ($IncludeScheduler) {
  foreach ($s in $SCHEDULER_FILES) {
    $src = Join-Path $Source $s
    if (Test-Path $src) { $plan += [pscustomobject]@{ Rel = $s; Src = $src } }
  }
}

if (-not $plan) { throw "Nada para copiar. O Source tem a pasta $COBRANCA_REL ?" }

# ── 2. Pré-checagem: arquivos críticos presentes no source ──────────────────
$cobrancaNames = (Resolve-RelFiles $Source $COBRANCA_REL | ForEach-Object { Split-Path $_.Rel -Leaf })
$missing = $REQUIRED_IN_SOURCE | Where-Object { $_ -notin $cobrancaNames }
if ($missing) {
  throw "Source incompleto — faltam em $COBRANCA_REL : $($missing -join ', '). Copie o bundle completo."
}

# ── 3. Preview do plano ─────────────────────────────────────────────────────
Write-Output "== Arquivos a copiar =="
foreach ($f in $plan) {
  $destFull = Join-Path $Dest $f.Rel
  $tag = if (Test-Path $destFull) { 'sobrescreve' } else { 'NOVO       ' }
  Write-Output ("  [$tag] $($f.Rel)")
}

# .env — diagnóstico
$envPath = Join-Path $Dest '.env'
$envHasKey = $false
if (Test-Path $envPath) {
  $envHasKey = [bool](Select-String -Path $envPath -Pattern "^\s*$THROTTLE_KEY\s*=" -Quiet)
  Write-Output ""
  Write-Output ("== .env == " + $(if ($envHasKey) { "$THROTTLE_KEY já presente (mantido)" } else { "vai ADICIONAR $THROTTLE_KEY=$THROTTLE_VALUE" }))
} else {
  Write-Warning ".env não encontrado em $Dest — verifique o deploy. Não vou criar do zero."
}

if (-not $Apply) {
  Write-Output ""
  Write-Output "(preview) Nada foi alterado. Rode novamente com -Apply para efetivar."
  return
}

# ── 4. Backup + cópia ───────────────────────────────────────────────────────
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $Dest "_deploy-backup\$stamp"
Write-Output ""
Write-Output "Backup do que for sobrescrito -> $backupRoot"

foreach ($f in $plan) {
  $destFull = Join-Path $Dest $f.Rel
  $destDir  = Split-Path $destFull -Parent
  if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null }
  if (Test-Path $destFull) {
    $bdir = Join-Path $backupRoot (Split-Path $f.Rel -Parent)
    New-Item -ItemType Directory -Force -Path $bdir | Out-Null
    Copy-Item $destFull -Destination $bdir -Force
  }
  Copy-Item $f.Src -Destination $destFull -Force
  Write-Output "  copiado: $($f.Rel)"
}

# ── 5. .env — adiciona o throttle só se faltar (nunca reescreve o resto) ─────
if ((Test-Path $envPath) -and (-not $envHasKey)) {
  Copy-Item $envPath -Destination (Join-Path $backupRoot '.env') -Force
  Add-Content -Path $envPath -Value "" -Encoding utf8
  Add-Content -Path $envPath -Value "# Throttle anti-bloqueio Locaweb (reenvio/cobrança)" -Encoding utf8
  Add-Content -Path $envPath -Value "$THROTTLE_KEY=$THROTTLE_VALUE" -Encoding utf8
  Write-Output "  .env: adicionado $THROTTLE_KEY=$THROTTLE_VALUE"
}

# ── 6. Validação: o run.py importa? (pega send_core ausente) ─────────────────
Write-Output ""
Write-Output "== Validando imports (run.py + send_core) =="
$cobrancaDest = Join-Path $Dest $COBRANCA_REL
$pyCode = "import sys; sys.path.insert(0, r'$cobrancaDest'); import send_core, run; print('IMPORT_OK')"
$ok = $false
try {
  $out = & py -3 -c $pyCode 2>&1
  if ($LASTEXITCODE -eq 0 -and ($out -match 'IMPORT_OK')) { $ok = $true }
  else { Write-Warning ("Falha no import:`n" + ($out -join "`n")) }
} catch {
  Write-Warning "Não consegui rodar 'py -3' para validar: $_"
}
Write-Output ("Imports: " + $(if ($ok) { 'OK' } else { 'FALHOU — verifique o backup e a cópia (send_core.py presente?)' }))

# ── 7. Opcional: dry-run completo (imports + Firebird, sem enviar) ───────────
if ($Apply -and $DryRunAfter -and $ok) {
  Write-Output ""
  Write-Output "== Dry-run (não envia nada) =="
  & py -3 (Join-Path $cobrancaDest 'run.py') --dry-run
}

Write-Output ""
Write-Output ("Deploy concluído. Backup em: $backupRoot")
Write-Output "Próxima execução agendada usará o código novo automaticamente (sem reiniciar nada)."
