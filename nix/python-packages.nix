# Python package overrides: gw2mcp needs the official MCP SDK >= 2.3 (MCPServer API).
# If the nixpkgs revision in use still ships mcp 1.x, mcp 2.3.0 and its new
# dependencies (mcp-types, httpx2, httpcore2) are packaged from pinned PyPI wheels.
# Hashes are taken from uv.lock.
final: prev:
let
  inherit (prev.pkgs) lib;

  wheel =
    {
      pname,
      version,
      url,
      hash,
      dependencies ? [ ],
    }:
    final.buildPythonPackage {
      inherit pname version dependencies;
      format = "wheel";
      src = prev.pkgs.fetchurl { inherit url hash; };
      doCheck = false;
      dontCheckRuntimeDeps = true;
      pythonImportsCheck = [ ];
    };

  # Attribute *names* must not depend on `prev` values (that would cause infinite
  # recursion through callPackage), so every attribute is always defined and only
  # its value is chosen conditionally.
  needsMcp2 = !(prev ? mcp) || lib.versionOlder prev.mcp.version "2.3";
  pick = name: fallback: if needsMcp2 || !(prev ? ${name}) then fallback else prev.${name};
in
{
  gw2mcp = final.callPackage ./package.nix { };

  httpcore2 = pick "httpcore2" (wheel {
    pname = "httpcore2";
    version = "2.16.2";
    url = "https://files.pythonhosted.org/packages/2a/e8/009c347feac27f8960605aa98d55ed263b5870dd4fd80b82fd1d00d5716c/httpcore2-2.16.2-py3-none-any.whl";
    hash = "sha256-FgUa4qQvd2AHVD8y3pNJoxeyPSy7ThRWtLrA6IuHQZo=";
    dependencies = with final; [
      h11
      truststore
    ];
  });

  httpx2 = pick "httpx2" (wheel {
    pname = "httpx2";
    version = "2.16.2";
    url = "https://files.pythonhosted.org/packages/77/de/8c4306a3d6aa2495111986addf33a9e561eff4c47552e4c4b9251160d0ad/httpx2-2.16.2-py3-none-any.whl";
    hash = "sha256-8FwTqt5MRdvxyQYGR6EFjwrK1iOY+tludBJp7bM9RvU=";
    dependencies = with final; [
      anyio
      httpcore2
      idna
      truststore
      typing-extensions
    ];
  });

  mcp-types = pick "mcp-types" (wheel {
    pname = "mcp-types";
    version = "2.3.0";
    url = "https://files.pythonhosted.org/packages/ce/48/32583066b155bb5d4507e7887dc88ee23118cc4de8e2a12870c49719e2a8/mcp_types-2.3.0-py3-none-any.whl";
    hash = "sha256-lo79va7fqwatrkDTeKNDlfEJDFkh1L48nN4oOq922R0=";
    dependencies = with final; [
      pydantic
      typing-extensions
    ];
  });

  mcp = pick "mcp" (wheel {
    pname = "mcp";
    version = "2.3.0";
    url = "https://files.pythonhosted.org/packages/20/d7/4a2878f128df729db3ca04e71bfce26405cc2e0c9ebe19c592feeb4c61b9/mcp-2.3.0-py3-none-any.whl";
    hash = "sha256-3QxEwInRZFPorjGjh3oAVNeiMUyqqB9eBUG5sXNLI3c=";
    dependencies = with final; [
      anyio
      httpx2
      jsonschema
      mcp-types
      opentelemetry-api
      pydantic
      pyjwt
      cryptography
      python-multipart
      sse-starlette
      starlette
      typing-extensions
      typing-inspection
      uvicorn
    ];
  });
}
