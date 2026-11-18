# NixOS module for gw2mcp: a systemd service running the MCP server over
# streamable HTTP, with optional nginx reverse proxy (TLS via ACME).
#
# Example:
#
#   services.gw2mcp = {
#     enable = true;
#     apiKeyFile = "/run/secrets/gw2-api-key";     # GW2 API key (account, wallet, inventories, …)
#     authTokenFile = "/run/secrets/gw2mcp-token"; # bearer token MCP clients must send
#     nginx = {
#       enable = true;
#       virtualHost = "gw2.example.com";
#       enableACME = true;
#     };
#   };
{ self }:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.gw2mcp;
  inherit (lib)
    mkEnableOption
    mkIf
    mkOption
    mkPackageOption
    optional
    optionalAttrs
    types
    ;

  bool = b: if b then "true" else "false";

  upstream = "http://${if lib.hasInfix ":" cfg.host then "[${cfg.host}]" else cfg.host}:${toString cfg.port}";

  publicHosts = lib.unique (optional cfg.nginx.enable cfg.nginx.virtualHost ++ cfg.allowedHosts);

  isLoopback = lib.elem cfg.host [
    "localhost"
    "::1"
  ] || lib.hasPrefix "127." cfg.host;

  hasAuthToken =
    cfg.authTokenFile != null
    || cfg.environment ? GW2MCP_AUTH_TOKEN
    || cfg.environment ? GW2MCP_AUTH_TOKEN_FILE;
in
{
  options.services.gw2mcp = {
    enable = mkEnableOption "the Guild Wars 2 MCP server";

    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.gw2mcp;
      defaultText = lib.literalExpression "gw2mcp.packages.\${system}.gw2mcp";
      description = "The gw2mcp package to use.";
    };

    apiKeyFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/run/secrets/gw2-api-key";
      description = ''
        File containing the Guild Wars 2 API key (create one at
        https://account.arena.net/applications). Recommended permissions:
        account, inventories, characters, wallet, unlocks, progression,
        tradingpost, pvp, builds, wvw. Passed to the service as a systemd
        credential, so the file can be readable by root only. Without a key
        only public game data is available and no snapshots are taken.
      '';
    };

    authTokenFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/run/secrets/gw2mcp-token";
      description = ''
        File containing a bearer token that MCP clients must send
        (`Authorization: Bearer <token>`). Required when `host` is not a
        loopback address, and strongly recommended behind a reverse proxy.
        The service refuses to start if this file is empty.
      '';
    };

    host = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = "Address the HTTP server binds to.";
    };

    port = mkOption {
      type = types.port;
      default = 8765;
      description = "Port the HTTP server listens on.";
    };

    path = mkOption {
      type = types.strMatching "/.*";
      default = "/mcp";
      description = "URL path of the MCP streamable-HTTP endpoint.";
    };

    language = mkOption {
      type = types.enum [
        "en"
        "de"
        "es"
        "fr"
        "zh"
      ];
      default = "en";
      description = "Language for names returned by the GW2 API.";
    };

    snapshotInterval = mkOption {
      type = types.ints.unsigned;
      default = 3600;
      description = "Seconds between account snapshots for history tracking (0 disables).";
    };

    trackAccountValue = mkOption {
      type = types.bool;
      default = true;
      description = "Record the trading post value of all items with every snapshot.";
    };

    catalogTtlDays = mkOption {
      type = types.ints.positive;
      default = 7;
      description = "Days to cache public game data (items, achievements, …) locally.";
    };

    allowedHosts = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "gw2.internal:8765" ];
      description = ''
        Additional Host header values accepted by the server (DNS rebinding
        protection). The nginx virtual host is added automatically.
      '';
    };

    logLevel = mkOption {
      type = types.enum [
        "DEBUG"
        "INFO"
        "WARNING"
        "ERROR"
      ];
      default = "INFO";
      description = "Log level.";
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = "Open `port` in the firewall (only useful without nginx and with a non-loopback host).";
    };

    environment = mkOption {
      type = types.attrsOf types.str;
      default = { };
      description = "Extra environment variables for the service.";
    };

    nginx = {
      enable = mkEnableOption "an nginx reverse proxy in front of gw2mcp";

      virtualHost = mkOption {
        type = types.str;
        example = "gw2.example.com";
        description = "Name of the nginx virtual host serving the MCP endpoint.";
      };

      enableACME = mkOption {
        type = types.bool;
        default = false;
        description = "Obtain a TLS certificate for the virtual host via ACME.";
      };

      forceSSL = mkOption {
        type = types.bool;
        default = cfg.nginx.enableACME;
        defaultText = lib.literalExpression "config.services.gw2mcp.nginx.enableACME";
        description = "Redirect HTTP to HTTPS.";
      };

      extraLocationConfig = mkOption {
        type = types.lines;
        default = "";
        example = "allow 10.0.0.0/8; deny all;";
        description = "Extra nginx directives for the MCP location (e.g. IP allow lists).";
      };
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.nginx.enable -> cfg.nginx.virtualHost != "";
        message = "services.gw2mcp.nginx.virtualHost must be set when nginx is enabled.";
      }
      {
        assertion = isLoopback || hasAuthToken;
        message = "services.gw2mcp.authTokenFile must be set when services.gw2mcp.host is not a loopback address.";
      }
    ];

    warnings = optional (cfg.nginx.enable && cfg.authTokenFile == null) ''
      services.gw2mcp is exposed through nginx without services.gw2mcp.authTokenFile;
      anybody who can reach ${cfg.nginx.virtualHost} can read your Guild Wars 2 account data.
    '';

    systemd.services.gw2mcp = {
      description = "Guild Wars 2 MCP server";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];

      environment = {
        GW2MCP_TRANSPORT = "http";
        GW2MCP_HOST = cfg.host;
        GW2MCP_PORT = toString cfg.port;
        GW2MCP_HTTP_PATH = cfg.path;
        GW2MCP_LANG = cfg.language;
        GW2MCP_SNAPSHOT_INTERVAL = toString cfg.snapshotInterval;
        GW2MCP_SNAPSHOT_VALUE = bool cfg.trackAccountValue;
        GW2MCP_CATALOG_TTL_DAYS = toString cfg.catalogTtlDays;
        GW2MCP_LOG_LEVEL = cfg.logLevel;
        GW2MCP_DB = "/var/lib/gw2mcp/gw2mcp.sqlite3";
      }
      // optionalAttrs (publicHosts != [ ]) { GW2MCP_ALLOWED_HOSTS = lib.concatStringsSep "," publicHosts; }
      // cfg.environment;

      serviceConfig = {
        ExecStart = "${lib.getExe cfg.package} serve";
        Restart = "on-failure";
        RestartSec = 10;

        DynamicUser = true;
        StateDirectory = "gw2mcp";
        StateDirectoryMode = "0700";
        WorkingDirectory = "/var/lib/gw2mcp";
        # secrets are read from $CREDENTIALS_DIRECTORY/{gw2-api-key,auth-token}
        LoadCredential =
          optional (cfg.apiKeyFile != null) "gw2-api-key:${cfg.apiKeyFile}"
          ++ optional (cfg.authTokenFile != null) "auth-token:${cfg.authTokenFile}";

        # hardening
        CapabilityBoundingSet = "";
        AmbientCapabilities = "";
        LockPersonality = true;
        NoNewPrivileges = true;
        PrivateDevices = true;
        PrivateTmp = true;
        PrivateUsers = true;
        ProcSubset = "pid";
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectHome = true;
        ProtectHostname = true;
        ProtectKernelLogs = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        ProtectProc = "invisible";
        ProtectSystem = "strict";
        RemoveIPC = true;
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [
          "@system-service"
          "~@privileged"
        ];
        UMask = "0077";
      };
    };

    networking.firewall.allowedTCPPorts = mkIf cfg.openFirewall [ cfg.port ];

    services.nginx = mkIf cfg.nginx.enable {
      enable = true;
      recommendedProxySettings = lib.mkDefault true;
      virtualHosts.${cfg.nginx.virtualHost} = {
        inherit (cfg.nginx) enableACME forceSSL;
        locations = {
          ${cfg.path} = {
            proxyPass = upstream;
            recommendedProxySettings = true;
            extraConfig = ''
              # streamable HTTP uses long-lived SSE responses
              proxy_http_version 1.1;
              proxy_set_header Connection "";
              proxy_buffering off;
              proxy_cache off;
              proxy_request_buffering off;
              chunked_transfer_encoding on;
              proxy_read_timeout 1h;
              proxy_send_timeout 1h;
              add_header X-Accel-Buffering no;
              client_max_body_size 4m;
              ${cfg.nginx.extraLocationConfig}
            '';
          };
          "= /healthz".proxyPass = upstream;
        };
      };
    };
  };
}
