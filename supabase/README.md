# Supabase

Schema changes live in `migrations/`, one SQL file per change, applied in filename order.
The Supabase GitHub integration (working directory `.`, Deploy to production on) applies them on push to `main`.
Backup: run the `Apply database migrations` workflow by hand.
Never edit the database by hand; add a new migration instead.

