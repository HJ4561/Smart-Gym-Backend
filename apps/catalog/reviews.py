"""Course and coach ratings count approved reviews only (members' reviews wait for an admin)."""
from django.db.models import Avg, Count


def refresh_course_rating(course):
    agg = course.reviews.filter(status='approved').aggregate(avg=Avg('rating'), n=Count('id'))
    course.rating = round(agg['avg'] or 0, 2)
    course.total_reviews = agg['n']
    course.save(update_fields=['rating', 'total_reviews'])


def refresh_trainer_rating(trainer):
    agg = trainer.reviews.filter(status='approved').aggregate(avg=Avg('rating'), n=Count('id'))
    trainer.rating = round(agg['avg'] or 0, 2)
    trainer.total_reviews = agg['n']
    trainer.save(update_fields=['rating', 'total_reviews'])


def check_review(data, partial=False):
    """{rating, comment} from a request → (values, errors)."""
    values, errors = {}, {}
    if 'rating' in data or not partial:
        try:
            rating = int(str(data.get('rating', '')).strip())
            if not 1 <= rating <= 5:
                raise ValueError
            values['rating'] = rating
        except (TypeError, ValueError):
            errors['rating'] = 'Choose a rating from 1 to 5 stars.'
    if 'comment' in data or not partial:
        comment = ' '.join(str(data.get('comment') or '').split())
        if len(comment) > 1000:
            errors['comment'] = 'Keep your review under 1000 characters.'
        values['comment'] = comment
    return values, errors
