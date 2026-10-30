Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'uvicorn|launcher\.py' } |
  Select-Object ProcessId, ParentProcessId, CreationDate, CommandLine |
  Format-List

Get-NetTCPConnection -LocalPort 8787 -State Listen | Select-Object OwningProcess, LocalAddress, LocalPort | Format-List

Get-CimInstance Win32_Process -Filter "ProcessId=23652" | Select-Object ProcessId, ParentProcessId, CreationDate, CommandLine | Format-List
