"""
Token usage tracking service for monitoring LLM API consumption and budget enforcement.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Tuple, Optional, Dict, List

from sqlalchemy import func, extract

from src.database import db
from src.models.token_usage import TokenUsage
from src.models.user import User

logger = logging.getLogger(__name__)


class TokenTracker:
    """Service for recording and checking token usage."""

    OPERATION_TYPES = [
        'summarization',
        'chat',
        'title_generation',
        'event_extraction',
        'query_routing',
        'query_enrichment'
    ]

    def record_usage(
        self,
        user_id: int,
        operation_type: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        model_name: str = None,
        cost: float = None
    ):
        """
        Record token usage - upserts into daily aggregate.

        Args:
            user_id: User ID who made the request
            operation_type: Type of operation (summarization, chat, etc.)
            prompt_tokens: Number of input tokens
            completion_tokens: Number of output tokens
            total_tokens: Total tokens (prompt + completion)
            model_name: Name of the model used
            cost: API cost if available (e.g., from OpenRouter)
        """
        try:
            today = date.today()

            # Find or create today's record for this user/operation
            usage = TokenUsage.query.filter_by(
                user_id=user_id,
                date=today,
                operation_type=operation_type
            ).first()

            if usage:
                # Update existing record
                usage.prompt_tokens += prompt_tokens
                usage.completion_tokens += completion_tokens
                usage.total_tokens += total_tokens
                usage.request_count += 1
                if cost:
                    usage.cost += cost
                if model_name:
                    usage.model_name = model_name  # Update to latest model used
            else:
                # Create new record
                usage = TokenUsage(
                    user_id=user_id,
                    date=today,
                    operation_type=operation_type,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    request_count=1,
                    model_name=model_name,
                    cost=cost or 0.0
                )
                db.session.add(usage)

            db.session.commit()
            logger.debug(f"Recorded {total_tokens} tokens for user {user_id}, operation {operation_type}")
            return usage

        except Exception as e:
            logger.error(f"Failed to record token usage: {e}")
            db.session.rollback()
            return None

    def get_monthly_usage(self, user_id: int, year: int = None, month: int = None) -> int:
        """Get total tokens used by a user in a given month."""
        if year is None:
            year = date.today().year
        if month is None:
            month = date.today().month

        result = db.session.query(func.sum(TokenUsage.total_tokens)).filter(
            TokenUsage.user_id == user_id,
            extract('year', TokenUsage.date) == year,
            extract('month', TokenUsage.date) == month
        ).scalar()

        return result or 0

    def get_monthly_cost(self, user_id: int, year: int = None, month: int = None) -> float:
        """Get total cost for a user in a given month."""
        if year is None:
            year = date.today().year
        if month is None:
            month = date.today().month

        result = db.session.query(func.sum(TokenUsage.cost)).filter(
            TokenUsage.user_id == user_id,
            extract('year', TokenUsage.date) == year,
            extract('month', TokenUsage.date) == month
        ).scalar()

        return result or 0.0

    def check_budget(self, user_id: int) -> Tuple[bool, float, Optional[str]]:
        """
        Check if user is within budget.

        Returns:
            (can_proceed, usage_percentage, message)
            - can_proceed: False if hard cap (100%) reached
            - usage_percentage: 0-100+
            - message: Warning/error message if applicable
        """
        try:
            user = db.session.get(User, user_id)
            if not user or not user.monthly_token_budget:
                return (True, 0, None)  # No budget = unlimited

            current_usage = self.get_monthly_usage(user_id)
            budget = user.monthly_token_budget
            percentage = (current_usage / budget) * 100

            if percentage >= 100:
                return (False, percentage,
                        f"Monthly token budget exceeded ({percentage:.1f}%). Contact admin for more tokens.")
            elif percentage >= 80:
                return (True, percentage,
                        f"Warning: {percentage:.1f}% of monthly token budget used.")
            else:
                return (True, percentage, None)

        except Exception as e:
            logger.error(f"Failed to check budget for user {user_id}: {e}")
            # Fail open - allow the request if we can't check
            return (True, 0, None)

    def get_daily_stats(self, days: int = 30, user_id: int = None) -> List[Dict]:
        """Get daily token usage for charts."""
        start_date = date.today() - timedelta(days=days - 1)

        query = db.session.query(
            TokenUsage.date,
            TokenUsage.operation_type,
            func.sum(TokenUsage.total_tokens).label('tokens'),
            func.sum(TokenUsage.cost).label('cost')
        ).filter(TokenUsage.date >= start_date)

        if user_id:
            query = query.filter(TokenUsage.user_id == user_id)

        results = query.group_by(TokenUsage.date, TokenUsage.operation_type).all()

        # Organize by date
        stats = {}
        for r in results:
            date_str = r.date.isoformat()
            if date_str not in stats:
                stats[date_str] = {'date': date_str, 'total': 0, 'cost': 0.0, 'by_operation': {}}
            stats[date_str]['total'] += r.tokens or 0
            stats[date_str]['cost'] += r.cost or 0
            stats[date_str]['by_operation'][r.operation_type] = r.tokens or 0

        # Fill in missing dates with zeros
        all_dates = []
        current = start_date
        while current <= date.today():
            date_str = current.isoformat()
            if date_str not in stats:
                stats[date_str] = {'date': date_str, 'total': 0, 'cost': 0.0, 'by_operation': {}}
            all_dates.append(date_str)
            current += timedelta(days=1)

        return [stats[d] for d in sorted(all_dates)]

    def get_monthly_stats(self, months: int = 12) -> List[Dict]:
        """Get monthly token usage for charts."""
        results = db.session.query(
            extract('year', TokenUsage.date).label('year'),
            extract('month', TokenUsage.date).label('month'),
            func.sum(TokenUsage.total_tokens).label('tokens'),
            func.sum(TokenUsage.cost).label('cost')
        ).group_by('year', 'month').order_by('year', 'month').all()

        # Get last N months
        monthly_data = [
            {
                'year': int(r.year),
                'month': int(r.month),
                'tokens': r.tokens or 0,
                'cost': r.cost or 0
            }
            for r in results
        ]

        return monthly_data[-months:] if len(monthly_data) > months else monthly_data

    def get_user_stats(self) -> List[Dict]:
        """Get per-user token usage breakdown for current month."""
        today = date.today()

        results = db.session.query(
            User.id,
            User.username,
            User.monthly_token_budget,
            func.sum(TokenUsage.total_tokens).label('usage'),
            func.sum(TokenUsage.cost).label('cost')
        ).outerjoin(
            TokenUsage,
            (User.id == TokenUsage.user_id) &
            (extract('year', TokenUsage.date) == today.year) &
            (extract('month', TokenUsage.date) == today.month)
        ).group_by(User.id).all()

        return [
            {
                'user_id': r.id,
                'username': r.username,
                'monthly_budget': r.monthly_token_budget,
                'current_usage': r.usage or 0,
                'cost': r.cost or 0,
                'percentage': ((r.usage or 0) / r.monthly_token_budget * 100)
                              if r.monthly_token_budget else 0
            }
            for r in results
        ]

    def get_today_usage(self, user_id: int = None) -> Dict:
        """Get today's token usage."""
        today = date.today()

        query = db.session.query(
            func.sum(TokenUsage.total_tokens).label('tokens'),
            func.sum(TokenUsage.cost).label('cost')
        ).filter(TokenUsage.date == today)

        if user_id:
            query = query.filter(TokenUsage.user_id == user_id)

        result = query.first()

        return {
            'tokens': result.tokens or 0,
            'cost': result.cost or 0
        }


# Singleton instance
token_tracker = TokenTracker()
