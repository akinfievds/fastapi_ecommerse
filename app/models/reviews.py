from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.database import Base


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    comment: Mapped[str | None] = mapped_column(String(300))
    comment_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    grade: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    user: Mapped["User"] = relationship("User", back_populates="reviews")
    products: Mapped["Product"] = relationship("Product", back_populates="reviews")

    @validates("grade")
    def validate_grade(self, key, value):
        if not 1 <= value <= 5:
            raise ValueError("Grade must be between 1 and 5")
        return value
