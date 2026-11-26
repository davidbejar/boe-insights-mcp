# Releasing

How a release of `boe-insights-mcp` is made. Pushing a tag that starts with `v` runs
`.github/workflows/release.yml`, which builds the package, publishes it to PyPI, pushes the
container image to GHCR, creates the GitHub release and (for final versions only) deploys the
documentation site.

## One-time setup

Do steps 1 and 2 before the first tag (including the `0.1.0rc1` pre-release). Steps 3 and 4 are
needed only before the first final version (`0.1.0`), because pre-releases do not deploy the site.

1. **GitHub environment `pypi`. Create it BEFORE the first tag.** GitHub creates a missing
   environment automatically, with no protection, so a tag pushed first would publish to PyPI
   without any review. In the repository open Settings -> Environments -> New environment, named
   `pypi`, then:
   - Tick "Required reviewers" and add the repository owner. Leave "Prevent self-review"
     UNTICKED: the owner is the only collaborator and could otherwise never approve a release.
   - Under "Deployment branches and tags" choose "Selected branches and tags" and add a rule of
     ref type Tag with the pattern `v*`, so only release tags can use the environment.
2. **PyPI trusted publisher.** On pypi.org, go to Your account -> Publishing and add a pending
   publisher with these values: project name `boe-insights-mcp`, owner `davidbejar`, repository
   `boe-insights-mcp`, workflow `release.yml`, environment `pypi`. No API token is used anywhere.
3. **Pages source** (before the first final release). Settings -> Pages -> "Build and
   deployment": set Source to "GitHub Actions".
4. **Let release tags deploy the site** (before the first final release). The `github-pages`
   environment restricts which refs may deploy. Open Settings -> Environments -> `github-pages`
   -> "Deployment branches and tags" (the "Deployment branches" dropdown, option "Selected
   branches and tags"), click "Add deployment branch or tag rule", set ref type to Tag and the
   name pattern to `v*`. Keep the existing rule for `main`. Without this, the site job is
   rejected.
5. **GHCR visibility** (after the first image push). The first push creates the package as
   private. Open the package (repository page -> Packages -> `boe-insights-mcp` -> Package
   settings) and change its visibility to Public so the image can be pulled without signing in.
   This cannot be undone: a public package cannot be made private again.
6. **Optional, recommended: protect the tags.** Settings -> Rules -> Rulesets -> New tag ruleset,
   target pattern `v*`, and enable "Restrict updates" and "Restrict deletions". A released tag
   then cannot be moved or removed.

## For each release

1. Create a branch and bump `version` in `pyproject.toml`, then run `uv lock` so `uv.lock`
   matches. Write the version in its normalised PEP 440 form (`0.1.0rc1`, not `0.1.0-rc1`); the
   workflow refuses anything else.
2. Update `CHANGELOG.md`: give the new version a section with its release date. The date is set
   in this pull request, because the tag goes on the merged commit.
3. Open a pull request and merge it once the checks pass.
4. Update `main`, then tag the merge commit and push the tag. The tag is the version with a `v`
   in front, for example `v0.1.0rc1` or `v0.1.0`:

   ```bash
   git switch main && git pull
   git tag v0.1.0
   git push origin v0.1.0
   ```

   The build job fails if the tag does not equal the version in `pyproject.toml`, or if the
   tagged commit is not on `main`.
5. In the Actions tab, open the Release run and check that the build job is green. Before you
   approve the `pypi` environment, confirm that the commit the run shows is the head of `main`
   and that CI is green on it. Then approve.
6. Wait for the remaining jobs (image, GitHub release and, for a final version, the site).

A version with an `a`, `b`, `rc` or `dev` part (for example `0.1.0rc1`) is a pre-release: the
GitHub release is marked as such, the image gets only its exact version tag (no `latest`, no
`MAJOR.MINOR`), and the documentation site is not deployed. Post-releases (`0.1.0.post1`) are
not supported by the workflow yet; release a new patch version instead.

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

PyPI version numbers can never be reused, even after deleting a release or a file.

- If the `pypi` job did not start publishing (the build failed, or you rejected the review), fix
  the problem and delete the tag (`git push origin :refs/tags/vX.Y.Z`; a tag ruleset, if you set
  one, must be lifted first), then tag again.
- If the PyPI upload fails part-way (some files uploaded, some not), that version is burned:
  release a new version.
- If the package was published and a later job (image, GitHub release, site) failed, use "Re-run
  failed jobs" in the Actions tab. Never use "Re-run all jobs": it runs the publish step again and
  fails with "File already exists".
- A re-run uses the workflow file and `Containerfile` as they were at the tag, so it can only
  recover transient failures (a network error, a registry outage). A bug in either needs a fix and
  a new version.

## Possible hardening later

Artifact attestations and an SBOM, digest-pinned base images in the `Containerfile`, and building
the image from the published wheel instead of from source.
