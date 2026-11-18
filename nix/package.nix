{
  lib,
  buildPythonPackage,
  hatchling,
  httpx,
  mcp,
  starlette,
  uvicorn,
  pytestCheckHook,
  pytest-asyncio,
}:

buildPythonPackage {
  pname = "gw2mcp";
  version = "0.1.0";
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../README.md
      ../src
      ../tests
    ];
  };

  build-system = [ hatchling ];

  dependencies = [
    httpx
    mcp
    starlette
    uvicorn
  ];

  # nixpkgs may lag behind the lower bounds used for development
  pythonRelaxDeps = [
    "httpx"
    "starlette"
    "uvicorn"
  ];

  nativeCheckInputs = [
    pytestCheckHook
    pytest-asyncio
  ];

  pythonImportsCheck = [
    "gw2mcp"
    "gw2mcp.server"
  ];

  meta = {
    description = "MCP server for the Guild Wars 2 API with historical account tracking";
    license = lib.licenses.mit;
    mainProgram = "gw2mcp";
    platforms = lib.platforms.unix;
  };
}
