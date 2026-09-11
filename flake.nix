{
  description = "Reusable PR end-to-end testing agent for Cua desktop sandboxes";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forSystems = nixpkgs.lib.genAttrs systems;
    in {
      packages = forSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          default = import ./nix { inherit pkgs; };
          e2e-test-agent = self.packages.${system}.default;
        });
      apps = forSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/e2e-test-agent";
        };
        action = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/e2e-test-agent-action";
        };
      });
      checks = forSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          package = self.packages.${system}.default;
          sdk-contract = pkgs.runCommand "e2e-test-agent-sdk-contract" {
            nativeBuildInputs = [ self.packages.${system}.default.pythonEnvironment ];
          } ''
            export HOME="$TMPDIR/home"
            export E2E_FLEET_SDK_CONTRACT=1
            mkdir -p "$HOME"
            cp -R ${./.} source
            chmod -R u+w source
            cd source
            python3 -m unittest discover -s tests -p test_claim_sdk_contract.py -v
            touch "$out"
          '';
          workflow = pkgs.runCommand "e2e-test-agent-workflows" {
            nativeBuildInputs = [ pkgs.actionlint ];
          } ''
            actionlint -shellcheck= ${./.github/workflows/ci.yml} ${./examples/e2e.yml}
            touch "$out"
          '';
          unit = pkgs.runCommand "e2e-test-agent-tests" {
            nativeBuildInputs = [ pkgs.python3 pkgs.git ];
          } ''
            export HOME="$TMPDIR/home"
            mkdir -p "$HOME"
            cp -R ${./.} source
            chmod -R u+w source
            cd source
            python3 -m unittest discover -s tests -v
            touch "$out"
          '';
        });
    };
}
