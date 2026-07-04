# sqlens

An extremely fast SQL linter and formatter, written in Rust.

> Fast like Ruff, but semantic like a compiler.

`sqlens` helps you format, lint, and analyze SQL with an understanding of syntax, dialects, schemas, and types.

## Highlights

* **Fast**: built in Rust for low-latency CLI and editor workflows.
* **Semantic linting**: schema, type, and dialect-aware diagnostics.
* **Standard-first design**: built on a SQL standard core, extended by dialects.
* **Dialect-aware**: designed for PostgreSQL, MySQL, BigQuery, Snowflake, DuckDB, SQLite, and more.

## Getting Started

```sh
curl -LsSf https://sqlens.sh/install.sh | sh
```

Format SQL:

```sh
sqlens lint   # Lint all sql files in the current directory.
sqlens format # Format all sql files in the current directory.
```

Use a specific dialect:

```sh
sqlens lint . --dialect mysql
```

## Example

```sql
select users.id,orders.total
from users join orders on users.id=orders.user_id
where orders.total > 100
order by orders.created_at desc
```

becomes:

```sql
SELECT
    users.id,
    orders.total
FROM users
JOIN orders
    ON users.id = orders.user_id
WHERE orders.total > 100
ORDER BY orders.created_at DESC
```

## Semantic Analysis

With schema information, `sqlens` can detect issues that purely text-based tools cannot.

Examples:

```text
error[unknown-column]: column `orders.deleted_at` does not exist
warning[ambiguous-column]: column `id` is ambiguous
warning[non-portable-syntax]: syntax is not supported by the target dialect
```

## License

MIT
