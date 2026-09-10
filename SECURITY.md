# Security policy

## Report a vulnerability privately

Use GitHub's [private vulnerability reporting form](https://github.com/Hovborg/automation-lens/security/advisories/new) for suspected security vulnerabilities in Automation Lens. You can also open the repository's **Security** tab, choose **Advisories**, and select **Report a vulnerability**.

Do not disclose an unpatched vulnerability in a public issue or pull request. Ordinary bugs and feature requests belong in [Issues](https://github.com/Hovborg/automation-lens/issues).

Include the affected version or commit, your operating system and Python version, the expected and observed behavior, and a minimal reproduction using fictional data. Explain the potential impact and any conditions required to reproduce it.

Never include real Home Assistant access tokens, credentials, private registry exports, household automation files, addresses, or identifying device data, even in a private report. Use fake credentials, `example.invalid` URLs, and a small synthetic fixture instead. If a credential has been exposed, revoke it in the service that issued it.

## Supported versions

Security fixes target the latest published release, including pre-releases, and the `main` branch. Older releases do not receive separate security backports. Upgrade to the latest release before checking whether a reported issue still applies.

This is a community-maintained project. Reports are reviewed as maintainer time permits; there is no guaranteed response or remediation deadline. Confirmed issues will be handled privately while a fix is prepared, with a security advisory when appropriate.

## Security boundaries

Automation Lens is a local command-line tool operated by the person who supplies its arguments and environment. The operator chooses the authorized Home Assistant URL and the local data directory; these settings are trusted configuration. The contents of automation files and API responses remain untrusted input.

The project does not provide a hosted or multi-user service. A wrapper that accepts URLs, directory paths, or environment settings from other users must enforce its own access controls and validate those values before calling Automation Lens. Run the tool with ordinary user permissions and use a dedicated data directory that untrusted users cannot modify, including its files and symbolic links.

- Home Assistant access is read-only: REST reads and WebSocket registry listing. Analysis must not trigger actions or change configuration.
- Access tokens are supplied explicitly by the user. The application must not persist tokens in its analysis cache or forward them to redirected hosts.
- Automation files and cached JSON are inputs to analysis, not executable code. Reproduction cases must use synthetic data.
- Findings are static analysis leads. A successful analysis or a scan with no alerts is not proof that a Home Assistant installation is secure or that an automation behaves correctly at runtime.

## Repository checks

GitHub CodeQL scans Python and GitHub Actions. Dependabot checks Python dependencies and Actions references and proposes updates as pull requests. Secret scanning and push protection help detect supported credential patterns. These checks supplement review and testing; they do not guarantee that all vulnerabilities or secrets will be detected.
