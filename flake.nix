{
  description = "Zetra reproducible contributor shell";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.mkShellNoCC {
            name = "zetra-dev";
            packages = with pkgs; [
              python312
              uv
              just
              git
              mise
              nixfmt
              shellcheck
              actionlint
            ];
            shellHook = ''
              export UV_CACHE_DIR="$PWD/.tools/uv-cache"
              printf 'Zetra: just bootstrap; just check\n'
            '';
          };
        }
      );
      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt);
    };
}
