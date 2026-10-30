$since = (Get-Date).AddHours(-12)
$events = Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=$since} -ErrorAction SilentlyContinue |
  Where-Object { $_.ProviderName -in @('Application Error','Windows Error Reporting','.NET Runtime','Application Hang') }
foreach ($e in $events) {
  Write-Output ('----- ' + $e.TimeCreated.ToString('HH:mm:ss') + ' [' + $e.ProviderName + '] id=' + $e.Id)
  Write-Output ($e.Message.Substring(0, [Math]::Min(600, $e.Message.Length)))
}
Write-Output ('TOTAL=' + @($events).Count)
