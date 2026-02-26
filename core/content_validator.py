"""Content validation against business profile before posting."""

import re
import logging
from typing import Dict, List, Tuple
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


class ContentValidator:
    """Validates LLM-generated content against business profile facts.

    Checks:
    1. Product name accuracy (exact spelling)
    2. URL accuracy (correct domain)
    3. Forbidden phrases (never_say rules)
    4. Bot-like patterns
    5. Length appropriateness
    6. Pricing claim verification
    """

    BOT_PATTERNS = [
        # ── Generic openers (instant bot tell) ──
        (r"^(Great question|I totally agree|This is amazing|This!)", "generic opener"),
        (r"^(Hey there|Hi there|Hello there|Hey!)", "bot-like greeting"),
        (r"^(Absolutely|Definitely|Totally|Certainly|Exactly)[,!]", "LLM cliche opener"),
        (r"^(This resonates|This hits|This is so relatable|Love this)", "sycophantic opener"),
        (r"^(Well,|So,|Honestly,|Actually,|In my experience,)\s", "formulaic opener"),
        (r"^(As someone who|Speaking as a|Coming from a)", "role-declaration opener"),
        # ── AI self-reference ──
        (r"(As an AI|I'm an AI|As a language model)", "AI self-reference"),
        # ── Formatting tells ──
        (r"(!!+)", "excessive exclamation"),
        (r"(#\w+\s*){2,}", "hashtags"),
        (r"\*\*.+?\*\*.*\*\*.+?\*\*", "too much bold formatting"),
        (r"(?:^[-•] .+$\n?){3,}", "excessive bullet list"),
        (r"(?:^\d+[.)]\s.+$\n?){3,}", "numbered list"),
        # ── LLM structural tells ──
        (r"(?i)in\s+(?:conclusion|summary|short)[,:]", "essay conclusion"),
        (r"(?i)let me\s+(?:break this down|explain|elaborate|walk you)", "LLM explanation"),
        (r"(?i)here'?s\s+(?:the thing|what I think|my take|the deal)[,:]", "formulaic transition"),
        (r"(?i)(?:that being said|with that said|having said that|not to mention)", "transition cliche"),
        (r"(?i)(?:on top of that|to add to this|building on this|to piggyback)", "stacking transition"),
        # ── Corporate / marketing language ──
        (r"(?i)\b(?:leverage|utilize|streamline|optimize|maximize)\s+(?:your|the|this)\b", "corporate language"),
        (r"(?i)\bgame[- ]?changer\b", "marketing cliche"),
        (r"(?i)\b(?:next level|level up|up your game|take it to)\b", "hype phrase"),
        (r"(?i)\b(?:don'?t sleep on|hidden gem|you won'?t regret|must[- ]have)\b", "promotional cliche"),
        (r"(?i)\b(?:robust|seamless|comprehensive|cutting[- ]edge|innovative)\b", "corporate adjective"),
        (r"(?i)\b(?:landscape|paradigm|synergy|ecosystem|holistic)\b", "corporate noun"),
        # ── AI hedging / service phrases ──
        (r"(?i)\bit(?:'?s| is) worth (?:noting|mentioning|pointing out)\b", "AI hedging phrase"),
        (r"(?i)\b(?:I'?d be happy to|feel free to|don't hesitate to)\b", "AI service phrase"),
        (r"(?i)\b(?:it'?s important to (?:note|remember|understand))\b", "AI didactic phrase"),
        (r"(?i)\b(?:I would (?:recommend|suggest|argue|say) that)\b", "AI hedging recommendation"),
        # ── Bot-like closers ──
        (r"(?i)(?:Hope this helps|Happy to help|Good luck|You'?ve got this)[!.]?\s*$", "bot-like closer"),
        (r"(?i)(?:Let me know if|Feel free to ask|Happy coding|Cheers!)\s*$", "bot-like closer"),
        (r"(?i)(?:Best of luck|Wishing you|All the best)[!.]?\s*$", "bot-like closer"),
        # ── Unnatural superlatives ──
        (r"(?i)\b(?:incredibly|remarkably|phenomenally|extraordinarily|insanely)\s+(?:useful|helpful|powerful|important|good)\b", "unnatural superlative"),
        # ── AI empathy / validation ──
        (r"(?i)^(?:I completely understand|I hear you|That's a great point|I can relate)", "AI empathy opener"),
        (r"(?i)^(?:What a great|Such a great|Really great)\s+(?:question|post|point|topic)", "AI validation opener"),
        # ── AI favorite words ──
        (r"(?i)\bdelve\s+(?:into|deeper)\b", "LLM favorite word 'delve'"),
        (r"(?i)\b(?:straightforward|arguably|nuanced|multifaceted)\b", "LLM favorite word"),
        (r"(?i)\b(?:a plethora of|a myriad of|a wealth of)\b", "LLM quantity phrase"),
        # ── Repetitive sentence starts ──
        (r"(?:^|\n)(I [a-z]+[^.]*\.\s*I [a-z]+[^.]*\.\s*I [a-z]+)", "3+ sentences starting with I"),
    ]

    def validate(
        self, content: str, project: Dict, platform: str = "reddit"
    ) -> Tuple[bool, float, List[str]]:
        """Validate content against business profile.

        Returns:
            (is_valid, score, issues)
            - is_valid: True if content passes all critical checks
            - score: 0.0-1.0 quality score
            - issues: list of issue descriptions
        """
        issues = []
        score = 1.0
        proj = project.get("project", project)
        profile = proj.get("business_profile", {})

        # Check 1: Product name accuracy
        name_issues = self._check_product_name(content, proj)
        issues.extend(name_issues)
        score -= len(name_issues) * 0.15

        # Check 2: URL accuracy
        url_issues = self._check_url(content, proj)
        issues.extend(url_issues)
        score -= len(url_issues) * 0.2

        # Check 3: Forbidden phrases
        forbidden_issues = self._check_forbidden(content, profile)
        issues.extend(forbidden_issues)
        score -= len(forbidden_issues) * 0.25

        # Check 4: Bot-like patterns
        bot_issues = self._check_bot_patterns(content)
        issues.extend(bot_issues)
        score -= len(bot_issues) * 0.1

        # Check 5: Length appropriateness
        length_issues = self._check_length(content, platform)
        issues.extend(length_issues)
        score -= len(length_issues) * 0.05

        # Check 6: Pricing claims
        if profile:
            claim_issues = self._check_pricing_claims(content, proj, profile)
            issues.extend(claim_issues)
            score -= len(claim_issues) * 0.15

        score = max(0.0, min(1.0, score))
        is_valid = score >= 0.5 and not any("CRITICAL" in i for i in issues)

        if issues:
            logger.info(
                f"Content validation: score={score:.2f}, issues={issues}"
            )
        else:
            logger.debug(f"Content validation: score={score:.2f}, clean")

        return is_valid, score, issues

    def _check_product_name(self, content: str, proj: Dict) -> List[str]:
        """Check if product name appears with correct spelling."""
        issues = []
        name = proj.get("name", "")
        if not name or len(name) < 2:
            return issues

        name_lower = name.lower()
        content_lower = content.lower()

        if name_lower not in content_lower:
            return issues  # Product not mentioned, that's fine

        # Find all occurrences case-insensitively and check capitalization
        for match in re.finditer(re.escape(name_lower), content_lower):
            start, end = match.start(), match.end()
            actual = content[start:end]
            if actual != name:
                issues.append(
                    f"CRITICAL: Product name misspelled: '{actual}' "
                    f"should be '{name}'"
                )

        return issues

    def _check_url(self, content: str, proj: Dict) -> List[str]:
        """Check if URLs in content match the project URL."""
        issues = []
        correct_url = proj.get("url", "")
        if not correct_url:
            return issues

        correct_domain = (
            correct_url
            .replace("https://", "")
            .replace("http://", "")
            .rstrip("/")
        )

        # Find all URLs in content
        url_pattern = r"https?://[^\s\)\]>\"']+"
        found_urls = re.findall(url_pattern, content)

        for url in found_urls:
            domain = (
                url
                .replace("https://", "")
                .replace("http://", "")
                .split("/")[0]
            )
            # Check similarity — catch hallucinated close-but-wrong URLs
            similarity = SequenceMatcher(
                None, domain.lower(), correct_domain.lower()
            ).ratio()
            if similarity > 0.5 and domain.lower() != correct_domain.lower():
                issues.append(
                    f"CRITICAL: Wrong URL '{url}' "
                    f"(should be {correct_url})"
                )

        return issues

    def _check_forbidden(self, content: str, profile: Dict) -> List[str]:
        """Check for forbidden phrases from business profile rules."""
        issues = []
        rules = profile.get("rules", {})

        for phrase in rules.get("never_say", []):
            if phrase.lower() in content.lower():
                issues.append(
                    f"CRITICAL: Contains forbidden phrase: '{phrase}'"
                )

        return issues

    def _check_bot_patterns(self, content: str) -> List[str]:
        """Check for bot-like writing patterns."""
        issues = []
        for pattern, desc in self.BOT_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                issues.append(f"Bot-like pattern: {desc}")
        return issues

    def _check_length(self, content: str, platform: str) -> List[str]:
        """Check content length is appropriate."""
        issues = []
        word_count = len(content.split())

        if platform == "twitter" and len(content) > 280:
            issues.append(f"Tweet exceeds 280 chars ({len(content)})")
        elif platform == "reddit":
            if word_count < 10:
                issues.append(f"Comment too short ({word_count} words)")
            elif word_count > 300:
                issues.append(f"Comment too long ({word_count} words)")

        return issues

    def _check_pricing_claims(
        self, content: str, proj: Dict, profile: Dict
    ) -> List[str]:
        """Check that pricing claims match the business profile."""
        issues = []
        content_lower = content.lower()
        name_lower = proj.get("name", "").lower()

        # Only check if the product is actually mentioned
        if name_lower not in content_lower:
            return issues

        pricing = profile.get("pricing", {})
        if not pricing:
            return issues

        # If content says "free" but pricing model is not free/freemium
        if " free" in content_lower and pricing.get("model") not in (
            "free", "freemium"
        ):
            issues.append(
                "Claims product is free but pricing model is "
                f"'{pricing.get('model', 'unknown')}'"
            )

        # Check for price amounts that don't match known plans
        price_pattern = r"\$\d+(?:\.\d{2})?"
        mentioned_prices = re.findall(price_pattern, content)
        if mentioned_prices and pricing.get("paid_plans"):
            valid_prices = [
                p.get("price", "") for p in pricing["paid_plans"]
            ]
            for mp in mentioned_prices:
                if not any(mp in vp for vp in valid_prices):
                    issues.append(
                        f"Price {mp} not found in business profile"
                    )

        return issues
