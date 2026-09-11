# Windows Task Scheduler -- Setup

Configurar uma tarefa agendada para rodar `run.py` todos os dias as 10:00.

---

## Passo a passo (interface grafica)

1. Abrir **Agendador de Tarefas** (`taskschd.msc`)
2. Clicar em **Criar Tarefa...** (nao "Criar Tarefa Basica")

### Aba Geral
- Nome: `Pagamentos - Cobranca Vencidos`
- Descricao: `Envia emails de cobranca para titulos vencidos via Firebird + Supabase`
- Marcar: **Executar independentemente do usuario estar conectado**
- Marcar: **Executar com privilegios mais altos**
- Configurar para: `Windows 10` (ou a versao do servidor)

### Aba Disparadores
- Clique em **Novo...**
- Iniciar a tarefa: **Em uma agenda**
- Configuracoes: **Diariamente**
- Hora de inicio: `10:00:00`
- Recorrencia: a cada `1` dia(s)
- Marcar: **Habilitado**

### Aba Acoes
- Clique em **Novo...**
- Acao: **Iniciar um programa**
- Programa/script:
  ```
  C:\Sheild\Projetos\Claude\Contas a pagar\Pagamentos\.venv\Scripts\python.exe
  ```
- Adicionar argumentos:
  ```
  skills/cobranca-vencidos/scripts/run.py
  ```
- Iniciar em (pasta de trabalho):
  ```
  C:\Sheild\Projetos\Claude\Contas a pagar\Pagamentos
  ```

### Aba Condicoes
- Desmarcar: **Iniciar a tarefa somente se o computador estiver com alimentacao CA**

### Aba Configuracoes
- Marcar: **Executar a tarefa o mais rapido possivel se um inicio agendado for perdido**
- Marcar: **Se a tarefa falhar, reiniciar a cada:** `1 hora` -- Tentar novamente: `2` vezes

---

## Verificar se esta funcionando

```powershell
Get-ScheduledTaskInfo -TaskName "Pagamentos - Cobranca Vencidos"
Start-ScheduledTask -TaskName "Pagamentos - Cobranca Vencidos"
```

---

## Logs

O script grava em `logs/cobranca_vencidos.log` (na raiz do projeto).
Rotacao automatica: mantem os ultimos 30 dias.

---

## Criacao via PowerShell (alternativa a interface grafica)

```powershell
$action = New-ScheduledTaskAction `
    -Execute "C:\Sheild\Projetos\Claude\Contas a pagar\Pagamentos\.venv\Scripts\python.exe" `
    -Argument "skills/cobranca-vencidos/scripts/run.py" `
    -WorkingDirectory "C:\Sheild\Projetos\Claude\Contas a pagar\Pagamentos"

$trigger = New-ScheduledTaskTrigger -Daily -At "10:00"

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName "Pagamentos - Cobranca Vencidos" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -Force
```
