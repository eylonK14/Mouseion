# Vendored browser runtime

- `htmx.min.js`: htmx 2.0.4, downloaded from the pinned upstream distribution
  (`https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`), Zero-Clause BSD.
- `../tailwind.css`: generated from `frontend/tailwind.input.css` with
  Tailwind CSS 3.4.17. Regenerate from the repository root with:

  ```bash
  npx --yes tailwindcss@3.4.17 -i frontend/tailwind.input.css \
    -o frontend/static/tailwind.css --minify \
    --content 'frontend/templates/**/*.html,frontend/static/*.js'
  ```
