# Supabase

Schema changes live in `migrations/`, one SQL file per change, applied in filename order.
The Supabase GitHub integration applies them to the production project on push to `main`.
Never edit the database by hand; add a new migration instead.

The production project is updated when a pull request into `main` is merged.
