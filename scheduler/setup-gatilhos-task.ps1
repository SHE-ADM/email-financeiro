<#
.SYNOPSIS
    Registra a tarefa MENSAL que mede os 7 gatilhos condicionais da Onda 9.

.DESCRIPTION
    Executa run_gatilhos.ps1 no dia 1 de cada mês, gravando a série em
    analytics.roadmap_trigger_snapshot. Rode como Administrador.

    Por que mensal, e não diário: nenhum gatilho vira de um dia para o outro — o valor está na
    SÉRIE, não no alerta. Medir todo dia só encheria a tabela com pontos idênticos.

.EXAMPLE
    .\setup-gatilhos-task.ps1
#>

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
$TASK_NAME    = "Pagamentos - Gatilhos Roadmap"
$TASK_PATH    = "\Sheild\"
$PROJECT_ROOT = Split-Path -Parent $PSScriptRoot
$RUNNER       = Join-Path $PSScriptRoot "run_gatilhos.ps1"

$TRIGGER_DAY  = 1      # dia do mês
$TRIGGER_H    = 7      # 07:00 — antes do expediente e longe das outras tarefas:
$TRIGGER_M    = 0      #   backup 02:00 · baixa 08:00 · cobrança 10:00 · reader de 5 em 5 min
$TIMEOUT_MIN  = 15     # a medição inteira leva segundos; 15 min é folga para rede ruim

# ---------------------------------------------------------------------------
# Validações
# ---------------------------------------------------------------------------
if (-not (Test-Path $RUNNER)) {
    Write-Error "Runner nao encontrado: $RUNNER"
    exit 1
}

$identidade = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal  = New-Object Security.Principal.WindowsPrincipal($identidade)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Execute este script como Administrador (Register-ScheduledTask exige elevacao)."
    exit 1
}

# ---------------------------------------------------------------------------
# Executor PowerShell — portabilidade entre máquinas
# ---------------------------------------------------------------------------
$psExe = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
if (-not $psExe) {
    $psExe = (Get-Command powershell.exe -ErrorAction SilentlyContinue).Source
}
if (-not $psExe) {
    Write-Error "Nenhum executor PowerShell encontrado (pwsh.exe nem powershell.exe)."
    exit 1
}

# ---------------------------------------------------------------------------
# Registro via XML — o formato nativo do Agendador
# ---------------------------------------------------------------------------
# 🔴 POR QUE XML, E NAO OS CMDLETS (nao regredir)
# A primeira versao montava o gatilho mensal pela classe CIM `MSFT_TaskMonthlyTrigger` e passava o
# objeto para `Register-ScheduledTask`. O objeto MONTA sem erro — verificado no dev, propriedade a
# propriedade — mas o registro falha na maquina de producao com:
#
#     Register-ScheduledTask: Parametro incorreto.
#
# Ou seja: o defeito NAO aparece ao construir o trigger, so ao registra-lo, e a mensagem nao diz
# qual parametro. Pior: como o `catch` do script cobria o registro, ele culpava a falta de
# privilegio — com a janela JA elevada. Diagnostico errado e o pior tipo de mensagem de erro.
#
# `New-ScheduledTaskTrigger` nao tem opcao mensal (so Once/Daily/Weekly/AtLogon/AtStartup), e
# `-Once -RepetitionInterval 30 dias` seria SUTILMENTE errado: 30 dias nao e um mes, e a medicao
# escorregaria alguns dias por ano ate cair fora do mes pretendido.
#
# O XML e o formato que o proprio Task Scheduler consome e exporta. `<ScheduleByMonth>` declara os
# dias e os meses por NOME, sem bitmask — nao ha o que interpretar errado —, e o mesmo XML funciona
# no Windows PowerShell 5.1 e no PowerShell 7.

$startAt = Get-Date -Day $TRIGGER_DAY -Hour $TRIGGER_H -Minute $TRIGGER_M -Second 0 -Millisecond 0
if ($startAt -lt (Get-Date)) { $startAt = $startAt.AddMonths(1) }

$MESES = @('January','February','March','April','May','June',
           'July','August','September','October','November','December')
$mesesXml = ($MESES | ForEach-Object { "          <$_ />" }) -join "`n"

# O comando e os argumentos entram no XML: escapar `&`, `<` e `>` e obrigatorio — um caminho com
# "&" (ex.: "C:\P&D\...") produziria um XML invalido e o mesmo "Parametro incorreto" de novo.
function ConvertTo-XmlText { param([string]$Texto) [System.Security.SecurityElement]::Escape($Texto) }

$argumentos = "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RUNNER`""
$usuario    = "$env:USERDOMAIN\$env:USERNAME"

# `PT{n}M` = duracao ISO-8601 (o formato do Agendador). `StartWhenAvailable` importa mais aqui que
# nas tarefas diarias: se a maquina estiver desligada no dia 1, uma tarefa diaria perde uma
# execucao entre muitas — esta perderia o MES inteiro, e a serie ficaria com um buraco que ninguem
# nota ate precisar da tendencia.
# 🔴 A ORDEM DOS NOS E FIXA, e `<Principal>` vive DENTRO de `<Principals>` (plural).
# A primeira versao punha `<Principal>` solto e na posicao errada; o Agendador recusou com
# "O XML da tarefa contem um no inesperado. (32,5):Principal" — mensagem que aponta a LINHA, mas
# nao diz qual e a ordem certa. A sequencia abaixo foi tirada de um `Export-ScheduledTask` REAL
# desta familia de Windows, nao do XSD publicado: e a que o proprio sistema gera e aceita.
#
#     RegistrationInfo -> Principals -> Settings -> Triggers -> Actions
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$(ConvertTo-XmlText "Mede os 7 gatilhos condicionais da Onda 9 e grava a serie em analytics.roadmap_trigger_snapshot. Somente leitura sobre o negocio. Dia $TRIGGER_DAY de cada mes. Credenciais: .env na raiz do projeto.")</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>$(ConvertTo-XmlText $usuario)</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT${TIMEOUT_MIN}M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$($startAt.ToString('yyyy-MM-ddTHH:mm:ss'))</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByMonth>
        <DaysOfMonth>
          <Day>$TRIGGER_DAY</Day>
        </DaysOfMonth>
        <Months>
$mesesXml
        </Months>
      </ScheduleByMonth>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>$(ConvertTo-XmlText $psExe)</Command>
      <Arguments>$(ConvertTo-XmlText $argumentos)</Arguments>
      <WorkingDirectory>$(ConvertTo-XmlText $PROJECT_ROOT)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

# Falha CEDO se o XML nao for sequer parseavel: o erro do Agendador ("Parametro incorreto") nao
# diz onde esta o problema, e um XML malformado daria exatamente a mesma mensagem.
try {
    $doc = [xml]$xml
} catch {
    Write-Error "XML gerado e invalido (bug neste script, nao no Agendador): $_"
    exit 1
}

# Confere a SEQUENCIA dos nos antes de mandar ao Agendador. Sem isto, uma reordenacao futura
# volta a produzir "no inesperado" — um erro que aponta a linha e nao explica a causa.
$ordemEsperada = @('RegistrationInfo', 'Principals', 'Settings', 'Triggers', 'Actions')
$ordemGerada   = @($doc.Task.ChildNodes | ForEach-Object { $_.Name })
if (Compare-Object $ordemEsperada $ordemGerada -SyncWindow 0) {
    Write-Error @"
Ordem dos nos do XML fora do esperado (bug neste script).
  esperado: $($ordemEsperada -join ' -> ')
  gerado  : $($ordemGerada -join ' -> ')
"@
    exit 1
}

try {
    Register-ScheduledTask -TaskName $TASK_NAME -TaskPath $TASK_PATH -Xml $xml -Force | Out-Null
} catch {
    Write-Error @"
Falha ao registrar a tarefa: $_

Verifique, nesta ordem:
  1. a janela do PowerShell esta ELEVADA (Executar como Administrador);
  2. o caminho do runner existe: $RUNNER
  3. em ultimo caso, o equivalente por schtasks:
       schtasks /create /tn "$TASK_NAME" /sc MONTHLY /d $TRIGGER_DAY /st $('{0:00}:{1:00}' -f $TRIGGER_H, $TRIGGER_M) /rl HIGHEST ``
         /tr "\"$psExe\" $argumentos"
"@
    exit 1
}

# ---------------------------------------------------------------------------
# Confirmação
# ---------------------------------------------------------------------------
$task = Get-ScheduledTask -TaskPath $TASK_PATH -TaskName $TASK_NAME -ErrorAction SilentlyContinue

# 🔴 Conferir o EFEITO do gatilho, nao o TIPO do objeto. A primeira versao exigia
# `CimClassName -eq "MSFT_TaskMonthlyTrigger"` e reprovou uma tarefa REGISTRADA CORRETAMENTE:
# quando o registro e feito por XML, o Agendador expoe o gatilho como `CalendarTrigger`, nao pela
# classe CIM que os cmdlets usariam. A verificacao acusou defeito onde nao havia — e mandou o
# operador remover uma tarefa que estava boa.
#
# `NextRunTime` e a prova que importa: ele so existe se houver gatilho ATIVO e agendavel. Vale
# para qualquer forma de registro, hoje e depois.
$info = Get-ScheduledTaskInfo -TaskPath $TASK_PATH -TaskName $TASK_NAME -ErrorAction SilentlyContinue

if ($task) {
    $qtdTriggers = @($task.Triggers).Count
    if ($qtdTriggers -lt 1 -or -not $info.NextRunTime) {
        Write-Error @"
Tarefa registrada, mas sem proxima execucao agendada (gatilhos: $qtdTriggers).
Remova com:
  Unregister-ScheduledTask -TaskPath '$TASK_PATH' -TaskName '$TASK_NAME' -Confirm:`$false
"@
        exit 1
    }
    Write-Host ""
    Write-Host ("Gatilho conferido: proxima execucao em " + $info.NextRunTime) -ForegroundColor DarkGray
}

if ($task) {
    Write-Host ""
    Write-Host "Tarefa registrada com sucesso!" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Caminho   : $TASK_PATH$TASK_NAME"
    Write-Host "  Executor  : $psExe"
    Write-Host "  Runner    : $RUNNER"
    Write-Host "  Proximo   : $($info.NextRunTime)"
    Write-Host "  Frequencia: dia $TRIGGER_DAY de cada mes, as $('{0:00}:{1:00}' -f $TRIGGER_H, $TRIGGER_M)"
    Write-Host "  Timeout   : $TIMEOUT_MIN minutos por execucao"
    Write-Host ""
    Write-Host "Para testar agora (sem aguardar o dia $TRIGGER_DAY):" -ForegroundColor Cyan
    Write-Host "  Start-ScheduledTask -TaskPath '$TASK_PATH' -TaskName '$TASK_NAME'"
    Write-Host ""
    Write-Host "Para medir sem gravar a serie:" -ForegroundColor Cyan
    Write-Host "  & '$RUNNER' -DryRun"
    Write-Host ""
    Write-Host "Logs em: $(Join-Path $PROJECT_ROOT 'logs\gatilhos\')"
} else {
    # 🔴 NAO culpar a elevacao aqui. O `Register-ScheduledTask` acima ja roda dentro de try/catch
    # com diagnostico proprio, e a checagem de Administrador acontece no inicio do script — se o
    # fluxo chegou ate aqui, a janela ESTAVA elevada. A primeira versao dizia "verifique se e
    # Administrador" para qualquer falha e mandou o operador procurar no lugar errado, com a
    # janela ja elevada: mensagem de erro que aponta a causa errada custa mais que nenhuma.
    Write-Error @"
A tarefa nao aparece no Agendador depois de registrada, e o registro nao acusou erro.

Isso e inesperado. Verifique:
  Get-ScheduledTask -TaskPath '$TASK_PATH' -TaskName '$TASK_NAME'
  Get-ScheduledTask | Where-Object TaskName -like '*Gatilhos*'   # foi parar em outro caminho?
"@
    exit 1
}
