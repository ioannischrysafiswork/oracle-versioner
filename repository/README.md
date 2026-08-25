# `repository/`

Generated output. **Do not edit by hand** — every file here is overwritten by
the agent from the current Oracle source.

Layout (docs/architecture.md §10.1):

```
repository/<DATABASE>/<SCHEMA>/<type_folder>/<OBJECT_NAME>.sql
```

`<type_folder>` is one of `packages`, `package_bodies`, `procedures`,
`functions`, `views`, `tables`, `types`, `type_bodies`, `triggers`,
`sequences`, `synonyms`, `materialized_views`.

Files are UTF-8 without BOM, LF line endings, with exactly one trailing
newline. The mapping between an object and its path is invertible; deleted-object
reconciliation depends on that, so paths are never hand-adjusted.
