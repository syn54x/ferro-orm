# /// script
# dependencies = [
#     "pydantic>=2.0",
#     "ferro-orm",
#     "rich",
# ]
# ///

import os
from datetime import datetime, timezone
from typing import Annotated

from pydantic import Field
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from ferro import (
    BackRelationship,
    FerroField,
    ForeignKey,
    ManyToManyField,
    Model,
    connect,
    transaction,
)

console = Console()


def show_step(title: str, code: str):
    """Utility to display a code snippet and its title."""
    console.print(f"\n[bold blue]>>> {title}[/bold blue]")
    syntax = Syntax(code, "python", theme="monokai", line_numbers=False)
    console.print(Panel(syntax, expand=False, border_style="dim"))


# 1. Define relationship-aware models
class Category(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
    # Reverse lookup marker (Zero-Boilerplate)
    products: BackRelationship[list["Product"]] = None


class Product(Model):
    id: Annotated[int | None, FerroField(primary_key=True, autoincrement=True)] = None
    name: Annotated[str, FerroField(index=True)]
    price: float
    # Foreign Key linking to Category
    category: Annotated[
        Category, ForeignKey(related_name="products", on_delete="CASCADE")
    ]
    in_stock: bool
    sku: Annotated[str | None, FerroField(unique=True)] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Actor(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
    movies: Annotated[list["Movie"], ManyToManyField(related_name="actors")] = None


class Movie(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: str
    actors: BackRelationship[list[Actor]] = None


async def run_demo():
    # Use a file-based SQLite DB for demo stability
    db_file = "demo.db"
    if os.path.exists(db_file):
        os.remove(db_file)

    console.print(
        Panel.fit(
            "[bold green]🚀 Ferro High-Performance ORM Demo[/bold green]",
            border_style="bold green",
        )
    )

    console.print(f"🚀 Connecting to Ferro Engine ({db_file})...")
    # connect() automatically resolves relationships and creates tables
    await connect(f"sqlite:{db_file}?mode=rwc", auto_migrate=True)

    # 2. Seeding with Relationships
    console.print("📦 Seeding initial data...")

    electronics = await Category.create(name="Electronics")
    appliances = await Category.create(name="Appliances")
    furniture = await Category.create(name="Furniture")

    data = [
        ("Laptop", 1200.0, electronics, True, "LPT-001"),
        ("Smartphone", 800.0, electronics, True, "PHN-001"),
        ("Headphones", 150.0, electronics, True, "HDP-001"),
        ("Monitor", 300.0, electronics, False, "MON-001"),
        ("Coffee Maker", 80.0, appliances, True, "COF-001"),
        ("Toaster", 30.0, appliances, True, "TST-001"),
        ("Desk Chair", 250.0, furniture, True, "CHR-001"),
        ("Bookshelf", 120.0, furniture, True, "BSH-001"),
        ("Mechanical Keyboard", 120.0, electronics, True, "KBD-001"),
        ("Gaming Mouse", 60.0, electronics, True, "MSE-001"),
    ]

    for name, price, cat, stock, sku in data:
        await Product(
            name=name, price=price, category=cat, in_stock=stock, sku=sku
        ).save()

    console.print("\n[bold yellow]--- 🔍 Running Fluent Queries ---[/bold yellow]")

    # 3. Filtering through relationships
    show_step(
        "Filter by Relationship ID",
        "results = await Product.where(Product.category_id == electronics.id).all()",
    )
    electronics_products = await Product.where(
        Product.category_id == electronics.id
    ).all()
    console.print(
        f"✅ Found [bold green]{len(electronics_products)}[/bold green] Electronics products."
    )

    # 4. Basic Filter (Operators)
    show_step(
        "Range Queries (>=)",
        "expensive = await Product.where(Product.price >= 500).all()",
    )
    expensive = await Product.where(Product.price >= 500).all()
    console.print(
        f"💰 Expensive items (>= 500): [cyan]{[p.name for p in expensive]}[/cyan]"
    )

    # 5. Chaining & Pagination
    show_step(
        "Pagination & Ordering",
        'ordered = await Product.select().order_by(Product.price, "desc").limit(3).all()',
    )
    top_3 = await Product.select().order_by(Product.price, "desc").limit(3).all()
    for p in top_3:
        console.print(f"🔝 [cyan]{p.name}[/cyan]: ${p.price}")

    # 6. RELATIONSHIP POWER
    console.print("\n[bold yellow]--- 🔗 Relationship Features ---[/bold yellow]")

    show_step(
        "Lazy Loading (Forward)",
        'laptop = await Product.where(Product.name == "Laptop").first()\n'
        "category = await laptop.category  # Fetched on demand",
    )
    laptop = await Product.where(Product.name == "Laptop").first()
    category = await laptop.category
    console.print(f"💻 Laptop Category: [bold green]{category.name}[/bold green]")

    show_step(
        "Reverse Lookup (Zero-Boilerplate)",
        'cat = await Category.where(Category.name == "Appliances").first()\n'
        "products = await cat.products.all()  # .products returns a Query object!",
    )
    app_cat = await Category.where(Category.name == "Appliances").first()
    app_products = await app_cat.products.all()
    console.print(f"🏠 Appliances found: [cyan]{[p.name for p in app_products]}[/cyan]")

    show_step(
        "Reverse Lookup with Filtering",
        "electronics_in_stock = await electronics.products.where(Product.in_stock == True).all()",
    )
    stock_electronics = await electronics.products.where(Product.in_stock == True).all()  # noqa
    console.print(
        f"📱 In-stock Electronics: [bold green]{len(stock_electronics)}[/bold green]"
    )

    # 7. MANY-TO-MANY (M2M)
    console.print("\n[bold yellow]--- 🤝 Many-to-Many Relationships ---[/bold yellow]")

    show_step(
        "M2M Setup & Mutation",
        'keanu = await Actor.create(name="Keanu Reeves")\n'
        'laurence = await Actor.create(name="Laurence Fishburne")\n'
        'matrix = await Movie.create(title="The Matrix")\n'
        "await keanu.movies.add(matrix)\n"
        "await laurence.movies.add(matrix)",
    )

    keanu = await Actor.create(name="Keanu Reeves")
    laurence = await Actor.create(name="Laurence Fishburne")
    matrix = await Movie.create(title="The Matrix")

    await keanu.movies.add(matrix)
    await laurence.movies.add(matrix)

    show_step(
        "M2M Querying (Bi-directional)",
        "keanu_movies = await keanu.movies.all()\n"
        "matrix_actors = await matrix.actors.all()",
    )

    keanu_movies = await keanu.movies.all()
    matrix_actors = await matrix.actors.all()

    console.print(f"🎬 Keanu's Movies: [cyan]{[m.title for m in keanu_movies]}[/cyan]")
    console.print(f"👥 Matrix Actors: [cyan]{[a.name for a in matrix_actors]}[/cyan]")

    # 8. Transactions
    console.print("\n[bold yellow]--- ⚛️ Transaction Support ---[/bold yellow]")

    show_step(
        "Atomic Transaction",
        "async with ferro.transaction():\n"
        '    new_cat = await Category.create(name="New Category")\n'
        '    await Product.create(name="Atomic Item", category=new_cat, ...)',
    )

    async with transaction():
        new_cat = await Category.create(name="Gaming")
        await Product.create(
            name="RTX 5090",
            price=1999.99,
            category=new_cat,
            in_stock=True,
            sku="GPU-5090",
        )

    gpu = await Product.where(Product.name == "RTX 5090").first()
    gpu_cat = await gpu.category
    console.print(
        f"✅ Transaction Committed: [bold green]{gpu.name}[/bold green] in [bold green]{gpu_cat.name}[/bold green]"
    )

    # 9. Instance Refreshing
    console.print("\n[bold yellow]--- 🔄 Instance Refreshing ---[/bold yellow]")
    show_step(
        "Refreshing an instance",
        "await Product.where(Product.id == laptop.id).update(price=50.0)\n"
        "await laptop.refresh()",
    )

    await Product.where(Product.id == laptop.id).update(price=50.0)
    await laptop.refresh()
    console.print(
        f"🔄 Laptop price after refresh: [bold green]${laptop.price}[/bold green]"
    )

    console.print("\n[bold green]🏁 Demo Complete![/bold green]")


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_demo())
