# Audible catalog API: field notes

The Audible apps use a JSON catalog API that answers product and search requests **without authentication**.
It is not officially documented; everything below was observed in practice (audible.de, October 2026).
For a full client including authenticated endpoints see the Python package [`audible`](https://audible.readthedocs.io/)
(endpoint overview under *misc/external_api*). These notes focus on what matters for matching a local collection.

Base URL per marketplace: `https://api.audible.de/1.0/catalog/products`
(also `api.audible.com`, `.co.uk`, `.fr`, `.it`, `.es`, `.ca`, `.com.au`, `.co.jp`, `.in`).

## Search

```
GET /1.0/catalog/products?title=Drangwäsche&author=Thurner
    &num_results=8&products_sort_by=Relevance
    &response_groups=product_attrs,contributors,series,product_desc
```

- **`title` + `author` is far more precise than `keywords`.** For numbered series (Perry Rhodan, Das Schwarze Auge, ...)
  keyword search returned unrelated volumes or box sets almost every time; title+author hit the exact product.
- `keywords` is still useful for vague queries (`keywords=Atlan Jagdplanet`).
- Up to 50 results per page (`num_results`), `page=` for more.

## response_groups matter

Without the right groups, fields are silently missing:

| You want | Add |
|---|---|
| `title`, `subtitle`, `publisher_summary` (full description, HTML) | `product_desc` (without it `title` can come back `None`) |
| authors, narrators | `contributors` |
| series name + sequence | `series` |
| release date, runtime, publisher, language, format | `product_attrs` |
| cover URLs | `media` plus `image_sizes=500,1024,2000` |
| genre ladders | `category_ladders` |
| parent/child links (series, box sets) | `relationships` |

`merchandising_summary` is a truncated teaser ("…"); use `publisher_summary`.

## Single product

```
GET /1.0/catalog/products/B0899LHLY2?response_groups=product_desc,product_attrs,contributors,series,media&image_sizes=1024
```

Batch lookup: `?asins=A,B,C` (40 at a time worked fine).

## Whole series in one go

1. Get the parent: `/products/<ASIN>?response_groups=relationships` → entry with
   `relationship_to_product == "parent"` and `relationship_type == "series"`.
2. Get the children: `/products/<PARENT>?response_groups=relationships,product_attrs` → entries with
   `relationship_to_product == "child"`, each with `sequence` and `sort`.
3. Fetch titles for the child ASINs in batches.

This maps an entire folder of numbered episodes to products at once, and it shows where Audible's numbering differs
from yours (it does: some series are renumbered or split, e.g. "Perry Rhodan 3050-3099" as series name without a sequence).

## Traps

- **Placeholder products**: release date `2200-01-01` and publisher `ZZZ - Series Advisor Placeholder`. Empty shells;
  ignore them and take data from neighbouring episodes.
- **Double releases**: some older radio plays exist only as a combined product (e.g. two episodes on one product).
- **Future release dates** for pre-orders.
- **Narrators listed as authors** on some radio plays; sanity-check against the folder name.
- Box sets ("Sammelband 1-3") rank high in searches; check `series.sequence` and runtime.

## Covers

Cover URLs look like `https://m.media-amazon.com/images/I/<id>._SL1024_.jpg`. The `_SL<n>_` part selects the size;
1024 is a good default (square, ~150 KB). The image host answers without authentication.

## Other sources that worked / did not (German titles)

| Source | Result |
|---|---|
| Audible API | primary source: 85 % of all books matched to a product |
| Wiki Aventurica (`de.wiki-aventurica.de`) | covers for *Das Schwarze Auge* audiobooks/radio plays; file pages link the full-size image under `/de/images/…` |
| OverDrive pages | reachable, usable covers |
| Deutsche Nationalbibliothek SRU | HTTP 429 for the whole session; do not rely on it from cloud IPs |
| Amazon product pages | captcha; unusable for scripts |
| lesejury / lismio | 503 / redirect |
