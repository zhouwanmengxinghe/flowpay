param([switch]$Offline, [string]$Proxy = "http://127.0.0.1:7890")
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Set-Location $projectRoot
$env:MAVEN_OPTS = "-Xmx768m -XX:ActiveProcessorCount=2"
$arguments = @("-B", "-ntp", "-f", "business/pom.xml")
if ($Offline) { $arguments += "-o" } else {
    $endpoint = [Uri]$Proxy
    if ($endpoint.Scheme -ne 'http') { throw 'Use an HTTP/mixed proxy URL.' }
    $env:HTTP_PROXY=$Proxy; $env:HTTPS_PROXY=$Proxy; $env:NO_PROXY='localhost,127.0.0.1,::1'
    New-Item -ItemType Directory -Force var | Out-Null
    $settings = Join-Path $projectRoot 'var/maven-proxy.xml'
    $xml = '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"><proxies><proxy><id>local-user-proxy</id><active>true</active><protocol>http</protocol><host>' + $endpoint.Host + '</host><port>' + $endpoint.Port + '</port><nonProxyHosts>localhost|127.0.0.1|::1</nonProxyHosts></proxy></proxies></settings>'
    [IO.File]::WriteAllText($settings,$xml,[Text.UTF8Encoding]::new($false))
    $arguments += @('-s',$settings)
}
& mvn @arguments package
if ($LASTEXITCODE -ne 0) { throw 'Java build failed.' }
