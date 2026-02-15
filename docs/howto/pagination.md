# How-To: Pagination

Efficient pagination is essential for handling large datasets. Ferro supports multiple pagination strategies.

## Offset-Based Pagination

The simplest approach uses `limit()` and `offset()`:

```python
from ferro import Model

class Product(Model):
    id: int
    name: str
    price: float

async def paginate_products(page: int = 1, per_page: int = 20):
    """Get a page of products."""
    offset = (page - 1) * per_page

    products = await Product.select() \
        .order_by(Product.id) \
        .limit(per_page) \
        .offset(offset) \
        .all()

    total = await Product.count()

    return {
        "items": products,
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": (total + per_page - 1) // per_page
    }

# Usage
result = await paginate_products(page=2, per_page=50)
print(f"Showing {len(result['items'])} of {result['total']} products")
```

### With Filtering

```python
async def search_products(
    query: str,
    page: int = 1,
    per_page: int = 20
):
    """Search and paginate products."""
    base_query = Product.where(Product.name.like(f"%{query}%"))

    products = await base_query \
        .order_by(Product.name) \
        .limit(per_page) \
        .offset((page - 1) * per_page) \
        .all()

    total = await base_query.count()

    return {"items": products, "total": total}
```

### Pros and Cons

**Pros:**
- Simple to implement
- Works with any column
- Users can jump to any page

**Cons:**
- Slow for large offsets (OFFSET 10000 is expensive)
- Inconsistent results if data changes between requests
- Database must scan and skip offset rows

## Cursor-Based Pagination

More efficient for large datasets. Uses the last seen ID as a cursor:

```python
async def paginate_cursor(after_id: int | None = None, limit: int = 20):
    """Cursor-based pagination using ID."""
    query = Product.select().order_by(Product.id)

    if after_id is not None:
        query = query.where(Product.id > after_id)

    products = await query.limit(limit).all()

    next_cursor = products[-1].id if products else None

    return {
        "items": products,
        "next_cursor": next_cursor,
        "has_more": len(products) == limit
    }

# Usage
page1 = await paginate_cursor(after_id=None, limit=20)
print(f"First page: {len(page1['items'])} items")

# Get next page
page2 = await paginate_cursor(after_id=page1['next_cursor'], limit=20)
print(f"Next page: {len(page2['items'])} items")
```

### With Multiple Sort Fields

```python
from datetime import datetime

async def paginate_cursor_advanced(
    after_timestamp: datetime | None = None,
    after_id: int | None = None,
    limit: int = 20
):
    """Cursor pagination with timestamp and ID."""
    query = Product.select() \
        .order_by(Product.created_at, "desc") \
        .order_by(Product.id, "desc")

    if after_timestamp and after_id:
        query = query.where(
            (Product.created_at < after_timestamp) |
            ((Product.created_at == after_timestamp) & (Product.id < after_id))
        )

    products = await query.limit(limit).all()

    if products:
        last = products[-1]
        return {
            "items": products,
            "next_cursor": {
                "timestamp": last.created_at,
                "id": last.id
            },
            "has_more": len(products) == limit
        }

    return {"items": [], "next_cursor": None, "has_more": False}
```

### Pros and Cons

**Pros:**
- Constant performance regardless of position
- Consistent results even if data changes
- Efficient for infinite scroll

**Cons:**
- Can't jump to arbitrary page
- More complex to implement
- Requires unique, sortable field

## Keyset Pagination

Similar to cursor-based, but uses any unique key:

```python
async def paginate_keyset(
    after_email: str | None = None,
    limit: int = 20
):
    """Keyset pagination using email."""
    query = User.select().order_by(User.email)

    if after_email:
        query = query.where(User.email > after_email)

    users = await query.limit(limit).all()

    return {
        "items": users,
        "next_key": users[-1].email if users else None,
        "has_more": len(users) == limit
    }
```

## FastAPI Integration

### Offset-Based

```python
from fastapi import FastAPI, Query
from pydantic import BaseModel

app = FastAPI()

class PaginatedResponse(BaseModel):
    items: list[Product]
    page: int
    per_page: int
    total: int
    pages: int

@app.get("/products", response_model=PaginatedResponse)
async def list_products(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100)
):
    return await paginate_products(page, per_page)
```

### Cursor-Based

```python
class CursorPaginatedResponse(BaseModel):
    items: list[Product]
    next_cursor: int | None
    has_more: bool

@app.get("/products/cursor", response_model=CursorPaginatedResponse)
async def list_products_cursor(
    cursor: int | None = Query(None),
    limit: int = Query(20, ge=1, le=100)
):
    return await paginate_cursor(after_id=cursor, limit=limit)
```

## Pagination Helper Class

Reusable pagination utility:

```python
from typing import Generic, TypeVar
from pydantic import BaseModel

T = TypeVar('T')

class Page(BaseModel, Generic[T]):
    items: list[T]
    page: int
    per_page: int
    total: int
    pages: int
    has_next: bool
    has_prev: bool

async def paginate(
    query,
    page: int = 1,
    per_page: int = 20
) -> Page:
    """Generic pagination helper."""
    total = await query.count()

    items = await query \
        .limit(per_page) \
        .offset((page - 1) * per_page) \
        .all()

    pages = (total + per_page - 1) // per_page

    return Page(
        items=items,
        page=page,
        per_page=per_page,
        total=total,
        pages=pages,
        has_next=page < pages,
        has_prev=page > 1
    )

# Usage
products_page = await paginate(
    Product.where(Product.active == True),
    page=2,
    per_page=50
)
```

## Performance Tips

### Always Order

```python
# Bad: Unpredictable results
products = await Product.limit(20).offset(40).all()

# Good: Consistent, predictable
products = await Product.order_by(Product.id).limit(20).offset(40).all()
```

### Index Sort Columns

```python
from ferro import FerroField

class Product(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    created_at: Annotated[datetime, FerroField(index=True)]  # Index for sorting
```

### Cache Counts

```python
from functools import lru_cache
from datetime import datetime, timedelta

_count_cache = {}

async def get_cached_count(model, cache_seconds=60):
    """Cache total count for pagination."""
    cache_key = model.__name__

    if cache_key in _count_cache:
        count, timestamp = _count_cache[cache_key]
        if datetime.now() - timestamp < timedelta(seconds=cache_seconds):
            return count

    count = await model.count()
    _count_cache[cache_key] = (count, datetime.now())
    return count

# Use in pagination
total = await get_cached_count(Product, cache_seconds=120)
```

### Limit Maximum Page Size

```python
MAX_PAGE_SIZE = 100

async def safe_paginate(page: int, per_page: int):
    """Enforce maximum page size."""
    per_page = min(per_page, MAX_PAGE_SIZE)
    # ... rest of pagination
```

## Which Strategy to Use?

**Use offset-based when:**
- Dataset is small (<10K records)
- Users need page numbers
- Jumping to specific pages is required
- Simplicity is prioritized

**Use cursor-based when:**
- Dataset is large (>10K records)
- Infinite scroll UI
- Real-time data feeds
- Performance is critical

**Use keyset when:**
- Sorting by non-ID fields
- Need stable pagination with filters
- Custom ordering requirements

## See Also

- [Queries](../guide/queries.md) - Filtering and ordering
- [Performance](../concepts/performance.md) - Query optimization
