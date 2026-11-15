# Agent guide for RGB MoCap

Read [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) before operating or changing this project. It covers installation, model selection, GUI workflows, retargeting, projects, resources, data protection and the main code paths. Consult the linked user and developer guides for detail.

- This is the Windows Git/CMD distribution. Preserve the separate pinned runtimes and model-specific behavior. GEM-X stays optional through `Setup_GEMX.cmd` and is not installed by `install.cmd` or `update.cmd`. Keep its Native77 data separate from SMPL, including contact correction and FBX export. Follow [docs/models/GEMX.md](docs/models/GEMX.md).
- Treat local `models/`, `projects/`, `cache/`, `character_scenes/`, runtime folders, external project folders and registered model data as user-owned. Do not delete, overwrite, upload or migrate them without appropriate user authorization. Ignore rules are not backups.
- Preserve inference and selected-result behavior when working on preview, contacts or export. Review the relevant code and validation records before changing numerical processing.
- Follow the user's request and perform checks appropriate to the change. Do not claim an inference, import, export, GPU, playback or clean-install test that was not actually run.
