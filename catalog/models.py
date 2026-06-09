from django.db import models


class Sport(models.Model):
    """A top-level sport, e.g. AFL or NRL."""

    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(max_length=50, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Competition(models.Model):
    """A specific competition within a sport, e.g. AFL, AFLW, NRL, NRLW."""

    sport = models.ForeignKey(Sport, on_delete=models.CASCADE, related_name="competitions")
    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(max_length=50, unique=True)
    is_womens = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Season(models.Model):
    """A playing season, identified by its year."""

    year = models.IntegerField(unique=True)
    label = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["-year"]

    def __str__(self):
        return self.label or str(self.year)
