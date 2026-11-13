from __future__ import annotations

from sqlalchemy import Column, Float, Integer, String

from .db import CorticoBase

# Read-only mirrors of the snapshot, holding only the columns this app reads.


class Conversation(CorticoBase):
    __tablename__ = "conversation"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer)
    title = Column(String, nullable=False)


class Highlight(CorticoBase):
    __tablename__ = "highlight"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, nullable=False)
    text = Column(String, nullable=False)
    start_offset = Column(Float, nullable=False)
    end_offset = Column(Float, nullable=False)
    review_status = Column(String, nullable=False)
