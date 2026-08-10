# The execution image for the `container` sandbox driver (ADR 0005).
#
# Two constraints shape everything here, and they pull against each other:
# the sandbox has no network, and the sandbox must be able to import packages.
# Those are only compatible if every package the agent could want is already in
# the image, so this file IS the package policy. Adding a library is an image
# rebuild, deliberately — a sandbox that can install things at runtime is a
# sandbox with egress, which is the thing we gave up on purpose.
#
# Nothing here needs to be small. It needs to be complete, because a missing
# import inside a network-isolated container is a dead end for the user rather
# than a `pip install` away.
FROM python:3.12-slim-bookworm

# Build-time only. The wheels below are manylinux, but a source build of one
# transitive dependency should not take the whole image down, and the compiler
# is removed in the same layer so it never reaches the runtime filesystem.
# Every pin below was resolved against the package index and then built, rather
# than written from memory. The first draft of this file pinned openpyxl==3.1.6,
# a version that has never existed, and the build failed on it — which is the
# cheap version of that mistake. The expensive version is a pin that resolves
# today and vanishes later, so when these are bumped, bump them by querying the
# index and rebuilding, not by editing the number.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && pip install --no-cache-dir \
        numpy==2.5.2 \
        pandas==3.0.5 \
        matplotlib==3.11.1 \
        scipy==1.18.0 \
        scikit-learn==1.9.0 \
        statsmodels==0.14.6 \
        sympy==1.14.0 \
        pyarrow==25.0.1 \
        openpyxl==3.1.5 \
        xlsxwriter==3.2.9 \
        Pillow==12.3.0 \
        python-dateutil==2.9.0.post0 \
        pytz==2026.3.post1 \
        requests==2.34.2 \
        beautifulsoup4==4.15.0 \
        lxml==6.1.1 \
        tabulate==0.10.0 \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

# requests and beautifulsoup4 are in that list even though the container has no
# network. They are there because generated code imports them reflexively, and
# an ImportError is a confusing failure where a connection error is an obvious
# one: the user learns "this sandbox has no internet" instead of "this sandbox
# is missing a library". The absence of a route is the enforcement, not the
# absence of a client library.

# Matplotlib must not try to reach a display, and it writes a font cache on
# first import — which fails on a read-only root filesystem unless it is pointed
# somewhere writable. Both are the kind of thing that shows up as a mystifying
# stack trace on the user's first plot.
ENV MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/mpl \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

# Warm the font cache at build time so the first plot in a fresh container is
# not several seconds slower than every subsequent one.
RUN python -c "import matplotlib.pyplot" 2>/dev/null || true

# 65534:65534 is nobody:nogroup. The container is additionally run with
# --read-only, --cap-drop ALL, --security-opt no-new-privileges, --network none
# and pid/memory/cpu limits; see services/sandbox/container_provider.py, which
# is the authority on the runtime flags. Setting a non-root user here as well
# means the image is not root-by-default if it is ever run by hand.
USER 65534:65534

WORKDIR /workspace

# No ENTRYPOINT on purpose. The provider passes an explicit argv, so the image
# cannot be coaxed into running something the provider did not name.
CMD ["python3", "-c", "print('jasmine sandbox image')"]
