{
  description = "MCP server for the Guild Wars 2 API with historical account tracking";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      pythonOverlay = import ./nix/python-packages.nix;
    in
    {
      overlays.default = final: prev: {
        pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [ pythonOverlay ];
        gw2mcp = final.python3Packages.toPythonApplication final.python3Packages.gw2mcp;
      };

      packages = forAllSystems (
        pkgs:
        let
          python = pkgs.python3.override {
            self = python;
            packageOverrides = pythonOverlay;
          };
        in
        {
          gw2mcp = python.pkgs.toPythonApplication python.pkgs.gw2mcp;
          default = self.packages.${pkgs.stdenv.hostPlatform.system}.gw2mcp;
        }
      );

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = nixpkgs.lib.getExe self.packages.${pkgs.stdenv.hostPlatform.system}.gw2mcp;
        };
      });

      nixosModules.default = import ./nix/module.nix { inherit self; };
      nixosModules.gw2mcp = self.nixosModules.default;

      checks = forAllSystems (
        pkgs:
        {
          package = self.packages.${pkgs.stdenv.hostPlatform.system}.gw2mcp;
        }
        // nixpkgs.lib.optionalAttrs pkgs.stdenv.isLinux {
          vm-test = import ./nix/test.nix { inherit self pkgs; };
        }
      );

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.python3
            pkgs.uv
            pkgs.ruff
          ];
          shellHook = ''
            export UV_PYTHON_DOWNLOADS=never
          '';
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt-rfc-style);
    };
}
