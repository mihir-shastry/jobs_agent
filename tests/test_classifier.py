"""Tests for title classification."""

import pytest

from jobagent.classifier import classify, classify_category, classify_level


class TestLevels:
    @pytest.mark.parametrize("title,expected", [
        ("Software Engineer Intern", "internship"),
        ("SWE Intern (Summer 2027)", "internship"),
        ("Backend Engineer Co-op", "internship"),
        ("Data Science Intern - Fall", "internship"),
        ("New Grad Software Engineer", "new_grad"),
        ("Software Engineer, New Grad (2027)", "new_grad"),
        ("Recent Grad - Full Stack", "new_grad"),
        ("Senior Software Engineer", "senior"),
        ("Staff Data Scientist", "senior"),
        ("Principal Engineer", "senior"),
        ("Engineering Manager", "senior"),
        ("Junior Developer", "entry"),
        ("Associate Product Manager", "entry"),
        ("Software Engineer II", "mid"),
        ("Mid-Level Backend Engineer", "mid"),
        ("Software Engineer", "entry"),  # default
    ])
    def test_level(self, title, expected):
        assert classify_level(title) == expected

    def test_intern_beats_senior(self):
        # "Senior Year Intern" style titles should still be internships.
        assert classify_level("Software Engineer Intern - Senior") == "internship"

    def test_roman_numeral_i_is_entry(self):
        assert classify_level("Product Analyst I") == "entry"


class TestCategories:
    @pytest.mark.parametrize("title,expected", [
        ("Software Engineer Intern", "swe"),
        ("Backend Engineer", "swe"),
        ("Frontend Developer", "swe"),
        ("Site Reliability Engineer", "swe"),
        ("Data Scientist", "data_science"),
        ("Machine Learning Engineer", "data_science"),
        ("ML Intern", "data_science"),
        ("Product Manager", "pm"),
        ("Technical Program Manager", "pm"),
        ("Product Designer", "design"),
        ("UX Researcher", "design"),
        ("Quantitative Researcher", "quant"),
        ("Quant Developer", "quant"),
        ("Recruiting Coordinator", "other"),
        ("Account Executive", "other"),
    ])
    def test_category(self, title, expected):
        assert classify_category(title) == expected


class TestClassify:
    def test_returns_both(self):
        level, category = classify("Software Engineering Intern")
        assert level == "internship"
        assert category == "swe"
