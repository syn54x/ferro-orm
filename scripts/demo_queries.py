# /// script
# dependencies = [
#     "pydantic>=2.0",
#     "ferro",
#     "rich",
# ]
# [tool.uv.sources]
# ferro = { path = ".." }
# ///

from asyncio import run
import os
from datetime import datetime, timezone
from typing import Annotated

from pydantic import Field
from rich.console import Console
from rich.syntax import Syntax
from rich.panel import Panel
from rich.table import Table

from ferro import FerroField, Model, connect

console = Console()


def show_step(title: str, code: str):
    """Utility to display a code snippet and its title."""
    console.print(f"\n[bold blue]>>> {title}[/bold blue]")
    syntax = Syntax(code, "python", theme="monokai", line_numbers=False)
    console.print(Panel(syntax, expand=False, border_style="dim"))


# 1. Define a high-performance model
class Product(Model):
    id: Annotated[int | None, FerroField(primary_key=True, autoincrement=True)] = Field(
        default=None
    )
    name: Annotated[str, FerroField(index=True)]
    price: float
    category: Annotated[str, FerroField(index=True)]
    in_stock: bool
    sku: Annotated[str | None, FerroField(unique=True)] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


async def run_demo():
    # Use a file-based SQLite DB for demo stability
    db_file = "demo.db"
    if os.path.exists(db_file):
        os.remove(db_file)
    
    console.print(Panel.fit("[bold green]🚀 Ferro High-Performance ORM Demo[/bold green]", border_style="bold green"))
    
    console.print(f"🚀 Connecting to Ferro Engine ({db_file})...")
    await connect(f"sqlite:{db_file}?mode=rwc", auto_migrate=True)

    # 3. Seed 10 items (High-Performance .save())
    console.print("📦 Seeding initial products...")
    data = [
        ("Laptop", 1200.0, "Electronics", True, "LPT-001"),
        ("Smartphone", 800.0, "Electronics", True, "PHN-001"),
        ("Headphones", 150.0, "Electronics", True, "HDP-001"),
        ("Monitor", 300.0, "Electronics", False, "MON-001"),
        ("Coffee Maker", 80.0, "Appliances", True, "COF-001"),
        ("Toaster", 30.0, "Appliances", True, "TST-001"),
        ("Desk Chair", 250.0, "Furniture", True, "CHR-001"),
        ("Bookshelf", 120.0, "Furniture", True, "BSH-001"),
        ("Mechanical Keyboard", 120.0, "Electronics", True, "KBD-001"),
        ("Gaming Mouse", 60.0, "Electronics", True, "MSE-001"),
    ]

    for name, price, cat, stock, sku in data:
        await Product(
            name=name, price=price, category=cat, in_stock=stock, sku=sku
        ).save()

    console.print("\n[bold yellow]--- 🔍 Running Fluent Queries ---[/bold yellow]")

    # 4. Basic Filter (Operators)
    show_step("Basic Filter (==)", 'electronics = await Product.where(Product.category == "Electronics").all()')
    electronics = await Product.where(Product.category == "Electronics").all()
    console.print(f"✅ Found [bold green]{len(electronics)}[/bold green] Electronics.")

    # 5. Range Queries (>=, <)
    show_step("Range Queries (>=)", 'expensive = await Product.where(Product.price >= 500).all()')
    expensive = await Product.where(Product.price >= 500).all()
    console.print(f"💰 Expensive items (>= 500): [cyan]{[p.name for p in expensive]}[/cyan]")

    # 6. The 'IN' Operator (<< Shorthand)
    show_step("The 'IN' Operator (<<)", 'subset = await Product.where(Product.name << ["Laptop", "Smartphone", "Monitor"]).all()')
    subset = await Product.where(
        Product.name << ["Laptop", "Smartphone", "Monitor"]
    ).all()
    console.print(f"📥 Batch Lookup: [cyan]{[p.name for p in subset]}[/cyan]")

    # 7. Chaining & Pagination (.limit, .offset)
    show_step("Pagination (.limit, .offset)", 'paged = await Product.where(Product.category == "Electronics").limit(2).offset(1).all()')
    paged = (
        await Product.where(Product.category == "Electronics").limit(2).offset(1).all()
    )
    console.print(f"📄 Electronics Page 2: [cyan]{[p.name for p in paged]}[/cyan]")

    # 7.1 Ordering & Counting
    show_step("Ordering & Counting", 'count = await Product.where(Product.category == "Electronics").count()\n'
                                     'ordered = await Product.where(...).order_by(Product.price, direction="desc").all()')
    
    electronics_count = await Product.where(Product.category == "Electronics").count()
    console.print(f"📱 Electronics count: [bold green]{electronics_count}[/bold green]")

    ordered = (
        await Product.where(Product.category == "Electronics")
        .order_by(Product.price, direction="desc")
        .all()
    )
    console.print(f"⬇️  Electronics (Price Desc): [cyan]{[(p.name, p.price) for p in ordered]}[/cyan]")

    # 8. Single Result (.first())
    show_step("Fetch Single Result (.first())", 'first_cheap = await Product.where(Product.price < 50).first()')
    first_cheap = await Product.where(Product.price < 50).first()
    console.print(f"🏷️ First item under $50: [bold cyan]{first_cheap.name if first_cheap else 'None'}[/bold cyan]")

    # 9. Complex Logic (OR/AND)
    show_step("Complex Logic (OR / AND)", 'query = (Product.category == "Appliances") | (Product.price > 1000)\n'
                                          'results = await Product.where(query).all()')
    complex_query = await Product.where(
        (Product.category == "Appliances") | (Product.price > 1000)
    ).all()
    console.print(f"🔹 (Appliances OR Price > 1000): [cyan]{[p.name for p in complex_query]}[/cyan]")

    # 10. SQL Injection Protection
    show_step("SQL Injection Protection", "injection = \"' OR '1'='1\"\n"
                                           "safe = await Product.where(Product.name == injection).first()")
    injection = "' OR '1'='1"
    safe = await Product.where(Product.name == injection).first()
    console.print(f"🛡️  SQL Injection check: {'[bold red]Bypassed![/bold red]' if safe else '[bold green]Safe (No results)[/bold green]'}")

    # 11. Deletion
    console.print("\n[bold yellow]--- 🗑️  Deletion ---[/bold yellow]")
    show_step("Instance Deletion", 'await toaster.delete()')
    toaster = await Product.where(Product.name == "Toaster").first()
    if toaster:
        await toaster.delete()
        check = await Product.where(Product.name == "Toaster").first()
        console.print(f"🔍 Toaster exists after delete? {'Yes' if check else '[bold green]No[/bold green]'}")

    show_step("Bulk Deletion", 'await Product.where(Product.category == "Appliances").delete()')
    deleted_count = await Product.where(Product.category == "Appliances").delete()
    console.print(f"✅ Deleted [bold red]{deleted_count}[/bold red] records.")

    # 12. Bulk Update
    console.print("\n[bold yellow]--- 📝 Bulk Update ---[/bold yellow]")
    show_step("Bulk Update", 'await Product.where(Product.category == "Electronics").update(price=999.99)')
    updated = await Product.where(Product.category == "Electronics").update(
        price=999.99
    )
    console.print(f"✅ Updated [bold green]{updated}[/bold green] electronics.")

    # 13. Convenience Helpers
    console.print("\n[bold yellow]--- 🛠️  Convenience Helpers ---[/bold yellow]")
    
    show_step(".exists()", 'await Product.where(Product.name == "Laptop").exists()')
    has_laptops = await Product.where(Product.name == "Laptop").exists()
    console.print(f"🧐 Do we have Laptops? {'[bold green]Yes[/bold green]' if has_laptops else 'No'}")

    show_step(".create()", 'new = await Product.create(name="VR Headset", ...)')
    new_product = await Product.create(
        name="VR Headset", price=499.99, category="Electronics", in_stock=True, sku="VR-001"
    )
    console.print(f"🆕 Created: [bold green]{new_product.name}[/bold green] (ID: {new_product.id})")

    show_step(".get_or_create()", 'mouse, created = await Product.get_or_create(name="Gaming Mouse")')
    mouse, created = await Product.get_or_create(name="Gaming Mouse")
    console.print(f"🤝 Mouse: [bold cyan]{mouse.name}[/bold cyan], Created? {created}")

    # 18. Instance Refreshing
    console.print("\n[bold yellow]--- 🔄 Instance Refreshing ---[/bold yellow]")
    show_step("Refreshing an instance", 'laptop = await Product.where(Product.name == "Laptop").first()\n'
                                         'await Product.where(Product.id == laptop.id).update(price=50.0)\n'
                                         '# laptop.price is still 999.99 in Python memory\n'
                                         'await laptop.refresh()\n'
                                         '# laptop.price is now 50.0')
    
    laptop = await Product.where(Product.name == "Laptop").first()
    old_price = laptop.price
    await Product.where(Product.id == laptop.id).update(price=50.0)
    
    console.print(f"💻 Laptop price in memory: [cyan]{old_price}[/cyan]")
    await laptop.refresh()
    console.print(f"🔄 Laptop price after refresh: [bold green]{laptop.price}[/bold green]")

    # 14. String Searching
    console.print("\n[bold yellow]--- 🔍 String Searching ---[/bold yellow]")
    
    show_step(".like()", 'results = await Product.where(Product.name.like("Lap%")).all()')
    laptops = await Product.where(Product.name.like("Lap%")).all()
    console.print(f"💻 Found Laptops: [cyan]{[p.name for p in laptops]}[/cyan]")

    show_step(".in_()", 'results = await Product.where(Product.category.in_(["Electronics", "Toys"])).all()')
    in_results = await Product.where(Product.category.in_(["Electronics", "Toys"])).all()
    console.print(f"📥 Found in Electronics or Toys: [bold green]{len(in_results)}[/bold green] products.")

    # 15. Temporal Types
    console.print("\n[bold yellow]--- ⏰ Temporal Types ---[/bold yellow]")
    
    show_step("Filtering by DateTime", 'now = datetime.now(timezone.utc)\n'
                                       'old_products = await Product.where(Product.created_at < now).all()')
    now = datetime.now(timezone.utc)
    old_products = await Product.where(Product.created_at < now).all()
    console.print(f"⌛ Found [bold green]{len(old_products)}[/bold green] products created before now.")
    if old_products:
        p = old_products[0]
        console.print(f"👤 Product: {p.name}, Created At: [cyan]{p.created_at}[/cyan] (Type: {type(p.created_at).__name__})")

    # 16. Structural Types
    console.print("\n[bold yellow]--- 🏗️ Structural Types ---[/bold yellow]")
    
    import uuid
    from decimal import Decimal
    show_step("UUID & Decimal Filtering", 'uid = uuid.uuid4()\n'
                                           'await Product.create(name="Special Item", sku=str(uid), price=99.99)\n'
                                           'item = await Product.where(Product.sku == uid).first()')
    
    # Use a real UUID for testing (sku is a string field but we can use UUID objects in queries)
    test_uid = uuid.uuid4()
    await Product.create(name="Special Item", sku=str(test_uid), price=99.99, category="Special", in_stock=True)
    
    special_item = await Product.where(Product.sku == test_uid).first()
    if special_item:
        console.print(f"🆔 Found by UUID: [bold green]{special_item.name}[/bold green]")
    
    show_step("Decimal Support", 'await Product.where(Product.price > Decimal("500.00")).all()')
    expensive_decimal = await Product.where(Product.price > Decimal("500.00")).all()
    console.print(f"💰 Found [bold green]{len(expensive_decimal)}[/bold green] expensive items using Decimal.")

    # 17. Transaction Support
    console.print("\n[bold yellow]--- ⚛️ Transaction Support ---[/bold yellow]")
    from ferro import transaction
    
    show_step("Atomic Transaction (Success)", 'async with ferro.transaction():\n'
                                               '    await Product.create(name="Atomic Item 1", ...)\n'
                                               '    await Product.create(name="Atomic Item 2", ...)')
    
    async with transaction():
        await Product.create(name="Atomic Item 1", price=10.0, category="Atomic", in_stock=True, sku="ATM-001")
        await Product.create(name="Atomic Item 2", price=20.0, category="Atomic", in_stock=True, sku="ATM-002")
    
    atomic_count = await Product.where(Product.category == "Atomic").count()
    console.print(f"✅ Transaction Committed: [bold green]{atomic_count}[/bold green] items created.")

    show_step("Atomic Transaction (Rollback)", 'try:\n'
                                               '    async with ferro.transaction():\n'
                                               '        await Product.create(name="Fail Item", ...)\n'
                                               '        raise ValueError("Rollback!")\n'
                                               'except ValueError: pass')
    
    try:
        async with transaction():
            await Product.create(name="Fail Item", price=0.0, category="Atomic", in_stock=True, sku="FAIL-001")
            raise ValueError("Intentional Failure")
    except ValueError:
        pass
    
    fail_check = await Product.where(Product.name == "Fail Item").exists()
    console.print(f"🛡️  Transaction Rolled Back: Fail Item exists? {'Yes' if fail_check else '[bold green]No[/bold green]'}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_demo())
