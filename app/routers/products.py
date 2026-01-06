from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_seller
from app.db_depends import get_async_db
from app.models.categories import Category as CategoryModel
from app.models.products import Product as ProductModel
from app.models.reviews import Review as ReviewModel
from app.models.users import User as UserModel
from app.schemas import Product as ProductSchema
from app.schemas import ProductCreate, ProductList
from app.schemas import Review as ReviewSchema

router = APIRouter(
    prefix="/products",
    tags=["products"],
)


@router.get("/", response_model=ProductList)
async def get_all_products(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category_id: int | None = Query(None, description="ID категории для фильтрации"),
    search: str | None = Query(
        None, min_length=1, description="Поиск по названию товара"
    ),
    min_price: float | None = Query(None, ge=0, description="Минимальная цена товара"),
    max_price: float | None = Query(None, ge=0, description="Максимальная цена товара"),
    in_stock: bool | None = Query(
        None, description="true - товары в наличии, false - только без остатка"
    ),
    seller_id: int | None = Query(None, description="ID продавца для фильтрации"),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Возвращает список всех активных товаров с поддержкой фильтров и поиска.
    """

    # Проверка логики min_price <= max_price
    if min_price and max_price and min_price > max_price:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="min_price не может быть больше max_price",
        )

    # Формируем список товаров
    filters = [ProductModel.is_active]

    if category_id:
        filters.append(ProductModel.category_id == category_id)
    if min_price:
        filters.append(ProductModel.price >= min_price)
    if max_price:
        filters.append(ProductModel.price <= max_price)
    if in_stock:
        filters.append(ProductModel.stock > 0 if in_stock else ProductModel.stock == 0)
    if seller_id:
        filters.append(ProductModel.seller_id == seller_id)

    # Подсчет общего количества с учетом фильтров
    total_stmt = select(func.count()).select_from(ProductModel).where(*filters)

    rank_col = None
    if search:
        search_value = search.strip()
        if search_value:
            # строим 2 tsquery для одной и той же фразы
            ts_query_en = func.websearch_to_tsquery("english", search_value)
            ts_query_ru = func.websearch_to_tsquery("russian", search_value)

            # Ищем совпадение в любой конфигурации и добавляем в общий фильтр
            ts_match_any = or_(
                ProductModel.tsv.op("@@")(ts_query_en),
                ProductModel.tsv.op("@@")(ts_query_ru),
            )
            filters.append(ts_match_any)

            # берем ранг максимальный из 2х
            rank_col = func.greatest(
                func.ts_rank_cd(ProductModel.tsv, ts_query_en),
                func.ts_rank_cd(ProductModel.tsv, ts_query_ru),
            ).label("rank")

            # total с учетом полнотекстового фильтра
            total_stmt = select(func.count()).select_from(ProductModel).where(*filters)

    total = await db.scalar(total_stmt) or 0

    # Основной запрос (если есть поиск - добавим ранг в выборку и сортировку)
    if rank_col is not None:  # TODO Почему нельзя использовать просто 'if rank_col:'
        products_stmt = (
            select(ProductModel, rank_col)
            .where(*filters)
            .order_by(desc(rank_col), ProductModel.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await db.execute(products_stmt)
        rows = result.all()
        items = [row[0] for row in rows]  # сами объекты
    else:
        products_stmt = (
            select(ProductModel)
            .where(*filters)
            .order_by(ProductModel.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = (await db.scalars(products_stmt)).all()

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.post("/", response_model=ProductSchema, status_code=status.HTTP_201_CREATED)
async def create_product(
    product: ProductCreate,
    db: AsyncSession = Depends(get_async_db),
    current_user: UserModel = Depends(get_current_seller),
):
    """
    Создаёт новый товар, привязанный к текущему продавцу (только для `seller`).
    """
    category_result = await db.scalars(
        select(CategoryModel).where(
            CategoryModel.id == product.category_id, CategoryModel.is_active
        )
    )
    if not category_result.first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category not found of innactive",
        )
    db_product = ProductModel(**product.model_dump(), seller_id=current_user.id)
    db.add(db_product)
    await db.commit()
    await db.refresh(db_product)
    return db_product


@router.get("/category/{category_id}", response_model=list[ProductSchema])
async def get_products_by_category(
    category_id: int, db: AsyncSession = Depends(get_async_db)
):
    """
    Возвращает список активных товаров в указанной категории по её ID.
    """
    # Проверяем, существует ли активная категория
    result = await db.scalars(
        select(CategoryModel).where(
            CategoryModel.id == category_id, CategoryModel.is_active
        )
    )
    category = result.first()
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found or inactive",
        )

    # Получаем активные товары в категории
    product_result = await db.scalars(
        select(ProductModel).where(
            ProductModel.category_id == category_id, ProductModel.is_active
        )
    )
    return product_result.all()


@router.get("/{product_id}/reviews", response_model=list[ReviewSchema])
async def get_reviews_by_product(
    product_id: int, db: AsyncSession = Depends(get_async_db)
):
    """
    Возвращает список активных отзывов по product_id.
    """
    # Проверяем, существует ли активный товар
    product_result = await db.scalars(
        select(ProductModel).where(
            ProductModel.id == product_id, ProductModel.is_active
        )
    )
    product = product_result.first()
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found or inactive",
        )

    reviews = await db.scalars(
        select(ReviewModel).where(
            ReviewModel.product_id == product_id, ReviewModel.is_active
        )
    )
    return reviews.all()


@router.get("/{product_id}", response_model=ProductSchema)
async def get_product(product_id: int, db: AsyncSession = Depends(get_async_db)):
    """
    Возвращает детальную информацию о товаре по его ID.
    """
    # Проверяем, существует ли активный товар
    product_result = await db.scalars(
        select(ProductModel).where(
            ProductModel.id == product_id, ProductModel.is_active
        )
    )
    product = product_result.first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found or inactive",
        )

    # Проверяем, существует ли активная категория
    category_result = await db.scalars(
        select(CategoryModel).where(
            CategoryModel.id == product.category_id, CategoryModel.is_active
        )
    )
    category = category_result.first()
    if not category:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category not found or inactive",
        )

    return product


@router.put("/{product_id}", response_model=ProductSchema)
async def update_product(
    product_id: int,
    product: ProductCreate,
    db: AsyncSession = Depends(get_async_db),
    current_user: UserModel = Depends(get_current_seller),
):
    """
    Обновляет товар, если он принадлежит текущему продавцу (только для `seller`)
    """
    result = await db.scalars(
        select(ProductModel).where(
            ProductModel.id == product_id, ProductModel.is_active
        )
    )
    db_product = result.first()
    if not db_product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Product not found"
        )
    if db_product.seller_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only update your own products",
        )
    category_result = await db.scalars(
        select(CategoryModel).where(
            CategoryModel.id == product.category_id, CategoryModel.is_active
        )
    )
    if not category_result.first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category not found or inactive",
        )
    await db.execute(
        update(ProductModel)
        .where(ProductModel.id == product_id)
        .values(**product.model_dump())
    )
    await db.commit()
    await db.refresh(db_product)
    return db_product


@router.delete("/{product_id}", response_model=ProductSchema)
async def delete_product(
    product_id: int,
    db: AsyncSession = Depends(get_async_db),
    current_user: UserModel = Depends(get_current_seller),
):
    """
    Выполняет мягкое удаление товара, если он принадлежит текущему продавцу (только для `seller`).
    """
    result = await db.scalars(
        select(ProductModel).where(
            ProductModel.id == product_id, ProductModel.is_active
        )
    )
    product = result.first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found or innactive",
        )
    if product.seller_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own proucts",
        )
    await db.execute(
        update(ProductModel)
        .where(ProductModel.id == product_id)
        .values(is_active=False)
    )
    await db.commit()
    await db.refresh(product)
    return product
