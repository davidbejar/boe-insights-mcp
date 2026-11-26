# Releasing

How a release of `boe-insights-mcp` is made. Pushing a tag that starts with `v` runs
`.github/workflows/release.yml`, which builds the package, publishes it to PyPI, pushes the
container image to GHCR, creates the GitHub release and (for final versions only) deploys the
documentation site.

## One-time setup

Do these once, before the first tag.

1. **PyPI trusted publisher.** On pypi.org, go to Your account -> Publishing and add a pending
   publisher with these values: project name `boe-insights-mcp`, owner `davidbejar`, repository
   `boe-insights-mcp`, workflow `release.yml`, environment `pypi`. No API token is used anywhere.
2. **GitHub environment `pypi`.** In the repository, Settings -> Environments -> New environment,
   named `pypi`. Under "Required reviewers" add yourself. Every PyPI publication then waits for
   your approval.
3. **Pages source.** Settings -> Pages -> "Build and deployment": set Source to "GitHub Actions".
4. **Let release tags deploy the site.** The `github-pages` environment restricts which refs may
   deploy. Open Settings -> Environments -> `github-pages` -> "Deployment branches and tags"
   (the "Deployment branches" dropdown, option "Selected branches and tags"), click "Add deployment
   branch or tag rule", set ref type to Tag and the name pattern to `v*`. Keep the existing rule
   for `main`. Without this, the site job of a final release is rejected.
5. **GHCR visibility.** The first image push creates the package as private. After the first
   release, open the package (repository page -> Packages -> `boe-insights-mcp` -> Package
   settings) and change its visibility to Public, so the image can be pulled without signing in.

## For each release

1. Create a branch and bump `version` in `pyproject.toml`, then run `uv lock` so `uv.lock`
   matches.
2. Update `CHANGELOG.md`: give the new version a section with its date.
3. Open a pull request and merge it once the checks pass.
4. Update `main`, then tag the merge commit and push the tag. The tag is the version with a `v`
   in front, for example `v0.1.0rc1` or `v0.1.0`:

   ```bash
   git switch main && git pull
   git tag v0.1.0
   git push origin v0.1.0
   ```

   The build job fails if the tag does not equal the version in `pyproject.toml`.
5. In the Actions tab, open the Release run, check the build job is green, then approve the
   `pypi` environment when GitHub asks for review.
6. Wait for the remaining jobs (image, GitHub release and, for a final version, the site).

A version with an `a`, `b`, `rc` or `dev` part (for example `0.1.0rc1`) is a pre-release: the
GitHub release is marked as such, the image gets only its exact version tag (no `latest`, no
`MAJOR.MINOR`), and the documentation site is not deployed.

## After the release, check

- Package: `uvx boe-insights-mcp@<version> --help` runs from PyPI. (`uvx` ignores pre-releases
  unless the version is given exactly, as here.) Check the PyPI project page shows the README
  with its logo.
- Image: `podman pull ghcr.io/davidbejar/boe-insights-mcp:<version>`, then run it as described in
  the `Containerfile` header and request `http://127.0.0.1:8000/health`.
- GitHub release: it exists for the tag, lists the sdist and wheel, and is marked pre-release when
  it should be.
- Site (final versions only): https://davidbejar.github.io/boe-insights-mcp/ shows the new build.

## If a release fails

PyPI version numbers can never be reused, even after deleting a release. If the package was
published (the `pypi` job ran) and something later is wrong, fix it and release a new version; do
not try to re-tag the same one. If the failure came before the `pypi` job published anything, you
can delete the tag (`git push origin :refs/tags/vX.Y.Z`), fix the problem and tag again. A failed
image, GitHub release or site job can be re-run from the Actions tab without a new version.
