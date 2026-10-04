# Security policy

oblako runs AWS-shaped services on your own machine for development and testing.
It is not meant to face a network: services listen on this machine only
(`127.0.0.1`), and the dashboard warns when you bind it elsewhere.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public issue.

Include what you found, how to reproduce it, and the oblako version
(`pip show oblako`). You will get a reply within a week. Fixes ship in the next
release, and the advisory credits you unless you prefer otherwise.

## Supported versions

Only the latest release receives security fixes.
