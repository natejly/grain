# Full TeX Live image for server-side LaTeX compilation.
#
# Unlike the Python sandbox (infra/sandbox/Dockerfile), this image IS the
# package policy for LaTeX. Every CTAN package in this frozen TeX Live year
# is available. The container runs with --network none, --read-only,
# --cap-drop ALL, and --security-opt no-new-privileges — the same isolation
# the Python sandbox uses.
#
# Shell escape is disabled at runtime (-no-shell-escape), so minted and
# anything else that calls out to an external process will not work.
FROM texlive/texlive:latest-full

# latexmk drives multi-pass compiles (bibliography, index, references).
# It is included in texlive-full but verify it is on the path.
RUN which latexmk && latexmk --version | head -1

ENV HOME=/tmp

USER 65534:65534
WORKDIR /workspace

CMD ["latexmk", "--version"]
