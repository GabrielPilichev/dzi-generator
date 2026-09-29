# DZI practical resource resolution

`practical_task_resources.resource_path` already stores either a repository-
relative file path or `repository-relative/archive.zip::member/path`. D2 uses
those existing references; it does not change metadata or extract ZIPs. The
runtime resolver is `src/practical_resource_resolution.py`.

Both the ordinary path and ZIP archive must resolve inside the configured
`data/reference` or `data/assets` roots. The archive and member paths reject
absolute, drive/UNC, backslash, `.`/`..`, empty-segment, control-character,
and non-NFC references. ZIP members are selected by exact name; ambiguous,
directory, symlink, encrypted, and unsupported-compression entries are
rejected. Only stored and deflated members are supported.

The current seed has 77 practical-resource records: 17 ordinary files and 60
ZIP members in five archives. The largest referenced archive is under 6 MB;
the largest referenced member is 2,945,849 bytes and the highest observed
compression ratio is below 1.5:1. Runtime limits leave room for future
reviewed resources: 64 MiB per archive, 10,000 archive entries, 16 MiB per
requested member, and a maximum 100:1 member compression ratio. Reading is
also capped to 16 MiB + 1 byte in case metadata understates the actual size.
No other archive entries are extracted or read.

The existing `/dzi/practical/resource/<id>/download` record lookup and
admin-only route guard remain authoritative. ZIP members are returned as
`application/octet-stream` attachments with `nosniff`; ordinary files retain
their existing download behavior. Invalid or unavailable records return 404
without exposing filesystem paths.
