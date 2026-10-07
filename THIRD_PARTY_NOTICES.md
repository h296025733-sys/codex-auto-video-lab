# Third-party notices

## FFmpeg

The original local toolchain uses an FFmpeg 8.1.2 Windows essentials build from gyan.dev. The executable reports a GPL version 3 or later license configuration. Its exact executable hashes are recorded in `config/toolchain.json`.

Before redistributing this workspace or packaging the binaries into a product, include the corresponding license materials and review the enabled codec/library obligations for the intended distribution model.

## Fine-cut fonts

The project-local fine-cut bundle includes Montserrat, Anton, and DM Serif Display from the official Google Fonts repository at pinned commit `ec626514f79f831f1ab848a82114a0ce7e2d6372`. Each family is distributed under the SIL Open Font License 1.1.

Exact upstream URLs, file sizes, SHA-256 hashes, roles, and family-specific license copies are stored in `.codex/skills/tiktok-fine-cut-director/assets/fonts/manifest.json`. These fonts are loaded from the workspace at render time and are not installed into Windows.


The public repository does not include FFmpeg executables or downloaded models.

## Fluent Emoji stickers

The four PNG stickers in `assets/edit-stickers/v1/` come from Microsoft's Fluent Emoji at the pinned revision listed in the manifest. Copyright Microsoft Corporation; MIT license. The original license and asset hashes are retained beside the files.
