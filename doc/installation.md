---
title: "Installation"
date: 2025-05-12
draft: false
weight: 10
description: "How to install the KCIDB package"
---
KCIDB requires Python v3.9 or later.

To install the package for the current user, run this command:

    pip3 install --user <SOURCE>

Where `<SOURCE>` is the location of the package source, e.g. a git repo:

    pip3 install --user git+https://github.com/kernelci/kcidb.git

or a directory path:

    pip3 install --user .

In any case, make sure your PATH includes the `~/.local/bin` directory, e.g.
with:

    export PATH="$PATH":~/.local/bin

## Legacy submission

Before you execute any of the tools make sure you have the path to your Google
Cloud credentials stored in the `GOOGLE_APPLICATION_CREDENTIALS` variable.
E.g.:

    export GOOGLE_APPLICATION_CREDENTIALS=~/.credentials.json

If you're representing a submitting CI system accessing the KCIDB service, you
will get your credentials from us. Otherwise you will need to create a service
account in your Google Cloud project, and download its key file to act as the
credentials.

## Submitting results

If you want to submit results from your CI system, you will need to install
the `kcidb` package and use the `kcidb-submit` command or `kcidb` Python module.
The `kcidb-submit` command is a wrapper around the `kcidb` module, which
provides a command-line interface for submitting results to the KCIDB service.

To authenticate, you will need to set the `KCIDB_REST` environment variable,
which should point to the KCIDB REST API endpoint. For example:

 export KCIDB_REST=https://YourToken@db.kernelci.org/submit

The `YourToken` part is the token you received from us. The `db.kernelci.org`
part is the KCIDB service endpoint. You can also set the `KCIDB_REST` variable
to point to a local KCIDB instance if you are running one. For example:

 export KCIDB_REST=http://localhost:8080/submit
