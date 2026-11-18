# NixOS VM test: gw2mcp behind nginx with bearer-token auth.
# Run with: nix flake check  (or nix build .#checks.x86_64-linux.vm-test)
{ self, pkgs }:
pkgs.testers.runNixOSTest {
  name = "gw2mcp";

  nodes.machine =
    { pkgs, ... }:
    {
      imports = [ self.nixosModules.default ];

      services.gw2mcp = {
        enable = true;
        # the VM has no internet access: the key is only used to check credential plumbing
        apiKeyFile = pkgs.writeText "gw2-api-key" "TEST-API-KEY";
        authTokenFile = pkgs.writeText "gw2mcp-token" "test-token";
        snapshotInterval = 0;
        nginx = {
          enable = true;
          virtualHost = "gw2.test";
        };
      };

      networking.extraHosts = "127.0.0.1 gw2.test";
      environment.systemPackages = [ pkgs.curl ];
    };

  testScript = ''
    import json

    INIT = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "vm-test", "version": "1"}},
    })
    HDRS = "-H 'content-type: application/json' -H 'accept: application/json, text/event-stream'"
    AUTH = "-H 'authorization: Bearer test-token'"

    def rpc_body(output):
        for line in output.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
        return json.loads(output.split("\r\n\r\n", 1)[-1])

    machine.wait_for_unit("gw2mcp.service")
    machine.wait_for_unit("nginx.service")
    machine.wait_for_open_port(8765)
    machine.wait_for_open_port(80)

    with subtest("health check is public"):
        machine.succeed("curl -sf http://gw2.test/healthz | grep -q ok")

    with subtest("MCP endpoint requires the bearer token"):
        code = machine.succeed(f"curl -s -o /dev/null -w '%{{http_code}}' {HDRS} -d '{INIT}' http://gw2.test/mcp")
        assert code.strip() == "401", code

    with subtest("unknown Host header is rejected"):
        code = machine.succeed(
            f"curl -s -o /dev/null -w '%{{http_code}}' {HDRS} {AUTH} -d '{INIT}' http://127.0.0.1:8765/mcp -H 'host: evil.test'"
        )
        assert code.strip() in ("400", "403", "421"), code

    with subtest("initialize and call a tool through nginx"):
        out = machine.succeed(f"curl -sf -D /tmp/headers {HDRS} {AUTH} -d '{INIT}' http://gw2.test/mcp")
        assert rpc_body(out)["result"]["serverInfo"]["name"] == "gw2mcp"
        headers = machine.succeed("cat /tmp/headers").lower()
        session = ""
        for line in headers.splitlines():
            if line.startswith("mcp-session-id:"):
                session = f"-H 'mcp-session-id: {line.split(':', 1)[1].strip()}'"
        proto = "-H 'mcp-protocol-version: 2025-06-18'"
        note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        machine.succeed(f"curl -s {HDRS} {AUTH} {session} {proto} -d '{note}' http://gw2.test/mcp")
        call = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "server_status", "arguments": {}}})
        out = machine.succeed(f"curl -sf {HDRS} {AUTH} {session} {proto} -d '{call}' http://gw2.test/mcp")
        status = rpc_body(out)["result"]["structuredContent"]
        assert status["api_key_configured"] is True, status
        assert status["snapshots"] == 0, status

    with subtest("state lives in /var/lib/gw2mcp"):
        machine.succeed("test -f /var/lib/private/gw2mcp/gw2mcp.sqlite3")
  '';
}
