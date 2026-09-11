{
  pkgs,
}:

let
  inherit (pkgs) lib;
  pythonPackages = pkgs.python3Packages;
  system = pkgs.stdenv.hostPlatform.system;
  wheelBySystem = {
    x86_64-linux = {
      url = "https://files.pythonhosted.org/packages/ac/29/c69780e1cb3acc5742979ca2761f3cd25240e06b592dad97624786fef0c1/claude_agent_sdk-0.2.149-py3-none-manylinux_2_17_x86_64.whl";
      hash = "sha256-vpPQxN3InawaIl3CPU1s3aDke3jvqKDKzJIBp0CpiFE=";
    };
    aarch64-linux = {
      url = "https://files.pythonhosted.org/packages/25/67/928b61afe78b633122f27b05b0e7642604aa5f9915fbcea039e7412625ab/claude_agent_sdk-0.2.149-py3-none-manylinux_2_17_aarch64.whl";
      hash = "sha256-KhTo+wXR9sJo+gQhDG211fG5aQJy0Awlgu9SYVx8Ylk=";
    };
    x86_64-darwin = {
      url = "https://files.pythonhosted.org/packages/f1/de/be9056efa75cf98cbd2a8dc0c868f7d44e17d36708bcb62495f25a276005/claude_agent_sdk-0.2.149-py3-none-macosx_11_0_x86_64.whl";
      hash = "sha256-6oI+ZBMv1qkjkCvERgOheQ7cE1fKEjCM5rZZUke5HfY=";
    };
    aarch64-darwin = {
      url = "https://files.pythonhosted.org/packages/ce/8a/e6118fb878360000694bbb7314e6c996a27df82df7d953682fd27456b056/claude_agent_sdk-0.2.149-py3-none-macosx_11_0_arm64.whl";
      hash = "sha256-iCY70udc63k+rnLj7n2Wd3oGQltF0ZO5LMuUQhDo6IU=";
    };
  };
  wheel = wheelBySystem.${system} or (throw "e2e-test-agent does not support ${system}");
  claudeAgentSdk = pythonPackages.buildPythonPackage {
    pname = "claude-agent-sdk";
    version = "0.2.149";
    format = "wheel";
    src = pkgs.fetchurl wheel;
    dependencies = with pythonPackages; [
      anyio
      jsonschema
      mcp
      sniffio
    ];
    nativeBuildInputs = lib.optionals pkgs.stdenv.isLinux [ pkgs.autoPatchelfHook ];
    buildInputs = lib.optionals pkgs.stdenv.isLinux [ pkgs.stdenv.cc.cc.lib ];
    dontStrip = true;
    doCheck = false;
    pythonImportsCheck = [ "claude_agent_sdk" ];
  };
  python = pkgs.python3.withPackages (_: [ claudeAgentSdk ]);
  cli = pkgs.writeShellApplication {
    name = "e2e-test-agent";
    runtimeInputs = [ python pkgs.ffmpeg pkgs.mlt pkgs.ripgrep pkgs.git pkgs.gh pkgs.gnutar ]
      ++ lib.optionals pkgs.stdenv.isLinux [ pkgs.bubblewrap ];
    text = ''
      export E2E_PLANNING_PROMPT="''${E2E_PLANNING_PROMPT:-${../agent/prompts/planning.md}}"
      export E2E_EXECUTION_PROMPT="''${E2E_EXECUTION_PROMPT:-${../agent/prompts/execution.md}}"
      export E2E_VIDEO_EDITING_PROMPT="''${E2E_VIDEO_EDITING_PROMPT:-${../agent/prompts/video-editing.md}}"
      export E2E_VIDEO_EDITOR="''${E2E_VIDEO_EDITOR:-${../agent/edit_video.py}}"
      exec ${python}/bin/python3 ${../agent/e2e_test_agent.py} "$@"
    '';
  };
  runner = pkgs.writeShellApplication {
    name = "e2e-test-agent-action";
    runtimeInputs = [ python cli pkgs.git ];
    text = ''
      exec ${python}/bin/python3 ${../scripts/run_action.py} "$@"
    '';
  };
in
pkgs.symlinkJoin {
  name = "e2e-test-agent";
  paths = [ cli runner ];
}
