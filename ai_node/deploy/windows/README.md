# Optional Windows / WSL orchestration

These PowerShell scripts manage a local LLM host and the WSL AI node. They are optional and assume LM Studio's `lms` CLI, a working WSL distribution and the [AI-node environment](../../README.md).

Copy `cube-settings.example.psd1` to `$env:USERPROFILE\CubeScripts\cube-settings.psd1` and replace all placeholders. `WslRepoPath` is the Linux checkout path, `WslDistro` must match `wsl --list`, `Model` must match your installed LLM, and `LlmHost` must be reachable from WSL. Keep this local data file out of Git. Review ports and trusted-network firewall rules.

Inspect both scripts and run them manually before installing tasks. `cube-ai-startup.ps1` starts the LLM service/model and WSL inference. **`cube-gaming-mode.ps1` intentionally stops model/service work and terminates WSL** to free resources; it can affect other workloads in that distribution. It is not a diagnostic command.

`install-cube-tasks.ps1` requires the settings file, copies the launchers into `CubeScripts`, and registers scheduled tasks. Run it only when those persistent changes are intended. Repository checks do not register tasks or restart processes.
