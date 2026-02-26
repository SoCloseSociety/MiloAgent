"""Content generation using LLM + prompt templates."""

import os
import random
import logging
from typing import Dict, List, Optional

from core.llm_provider import LLMProvider

logger = logging.getLogger(__name__)


class ContentGenerator:
    """Generates platform-specific content using LLM and prompt templates.

    Handles:
    - Loading prompt templates from prompts/ directory
    - Variable substitution (project, context, etc.)
    - 80/20 organic vs promotional ratio
    - Subreddit-aware persona selection
    - Post-type detection (question vs discussion vs help)
    - Rich business context injection for accurate product mentions
    """

    TONE_VARIATIONS = {
        "helpful_casual": [
            "Share a specific anecdote: 'ran into this exact thing last month when...' then the fix.",
            "Jump straight to the answer. No preamble. One concrete detail.",
            "Disagree slightly or offer a different angle: 'tbh I found the opposite...'",
            "Reference something specific from their post, then share your take.",
            "Ask a quick clarifying question first, then give your answer.",
        ],
        "tech_enthusiast": [
            "Name the specific tool/version you used and what happened. Be blunt.",
            "Compare two approaches you actually tried. Pick a winner. Say why.",
            "Point out something they might have missed. Be direct about it.",
            "Share a gotcha or edge case you hit. Save them the debugging time.",
        ],
        "industry_expert": [
            "Drop a non-obvious insight that only comes from doing this for years.",
            "Reference a real scenario (anonymized) where this exact thing came up.",
            "Politely challenge a common misconception in their post. Back it up.",
            "Give the boring but correct answer that nobody wants to hear.",
        ],
        "organic_engagement": [
            "React like you just scrolled past this and had to stop. Keep it brief.",
            "Share a quick thought — 1-2 sentences max. No agenda.",
            "Ask a genuine follow-up question. Something you actually want to know.",
            "Relate to their situation briefly. Maybe push back on one point.",
        ],
        "streamer_peer": [
            "Reference your own setup change that made a difference. Be specific.",
            "Use streaming vocabulary naturally: VODs, clips, raids, overlay, bitrate.",
            "Share a specific 'this fixed it for me' moment. Short and concrete.",
            "Commiserate about a common streaming frustration. Offer what worked.",
        ],
        "creator_mentor": [
            "Share a concrete number from your own growth and what caused it.",
            "Talk about a mistake you made and what you'd do differently now.",
            "Give them one specific thing to try today. Not generic advice.",
            "Push back gently if they're overcomplicating it. Simplify.",
        ],
    }

    # Map subreddits to persona style for more natural comments
    SUBREDDIT_PERSONAS = {
        "Twitch": {
            "tone": "streamer_peer",
            "persona": "Casual, uses streaming lingo. Knows about raids, VODs, clips, overlays. Supportive community vibe.",
        },
        "letsplay": {
            "tone": "streamer_peer",
            "persona": "Fellow Let's Player who understands the grind of recording, editing, and building an audience. Uses gaming terminology.",
        },
        "NewTubers": {
            "tone": "creator_mentor",
            "persona": "Experienced creator helping newcomers. Practical, encouraging, knows the struggle of starting out.",
        },
        "SmallYTChannel": {
            "tone": "creator_mentor",
            "persona": "Fellow small creator. Understands algorithm frustrations and growth challenges. Very supportive.",
        },
        "youtubers": {
            "tone": "creator_mentor",
            "persona": "Active YouTuber who discusses strategy, tools, and growth. Mix of practical and strategic advice.",
        },
        "contentcreation": {
            "tone": "tech_enthusiast",
            "persona": "Content creator who loves trying new tools and workflows. Always optimizing the creation process.",
        },
        "videography": {
            "tone": "industry_expert",
            "persona": "Professional videographer. Technical but accessible. Appreciates good gear and efficient workflows.",
        },
        "VideoEditing": {
            "tone": "industry_expert",
            "persona": "Video editor who knows NLEs inside out. Technical, detail-oriented, respects the craft.",
        },
        "streaming": {
            "tone": "streamer_peer",
            "persona": "Multi-platform streamer. Familiar with OBS, Streamlabs, alerts, clips. Chill and helpful.",
        },
    }

    DEFAULT_PERSONA = {
        "tone": "helpful_casual",
        "persona": "Helpful Redditor who gives genuine, specific advice. Casual tone, no corporate speak.",
    }

    # Expert domain personas for cross-platform use
    EXPERT_PERSONAS = {}  # Loaded from config/expert_personas.yaml

    @classmethod
    def load_expert_personas(cls):
        """Load expert persona configs from YAML."""
        import yaml
        personas_file = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "config", "expert_personas.yaml"
        )
        try:
            with open(personas_file, "r") as f:
                data = yaml.safe_load(f) or {}
            cls.EXPERT_PERSONAS = data.get("personas", {})
            logger.info(f"Loaded {len(cls.EXPERT_PERSONAS)} expert personas")
        except FileNotFoundError:
            logger.debug("No expert_personas.yaml found, using defaults")
        except Exception as e:
            logger.warning(f"Failed to load expert personas: {e}")

    def __init__(
        self,
        llm: LLMProvider,
        prompts_dir: str = "prompts/",
        organic_ratio: float = 0.8,
    ):
        self.llm = llm
        self.prompts_dir = prompts_dir
        self.organic_ratio = organic_ratio
        self.templates: Dict[str, str] = {}
        self._load_templates()

        # Set by orchestrator before generation (thread-local-ish)
        self._research_context: str = ""
        self._failure_rules: str = ""
        self._ab_engine = None  # Set by orchestrator for A/B testing
        self._db = None  # Set by orchestrator for evolved prompts

        self.load_expert_personas()

    def _load_templates(self):
        """Load all .txt prompt templates from the prompts directory."""
        if not os.path.isdir(self.prompts_dir):
            logger.warning(f"Prompts directory not found: {self.prompts_dir}")
            return

        for filename in os.listdir(self.prompts_dir):
            if filename.endswith(".txt") and not filename.startswith("tone_"):
                name = filename.replace(".txt", "")
                path = os.path.join(self.prompts_dir, filename)
                with open(path) as f:
                    self.templates[name] = f.read().strip()
                logger.debug(f"Loaded prompt template: {name}")

    def _should_be_promotional(self, ratio: Optional[float] = None) -> bool:
        """Returns True with probability (1 - organic_ratio), i.e. ~20%."""
        r = ratio if ratio is not None else (1.0 - self.organic_ratio)
        return random.random() < r

    def should_be_promotional(
        self, subreddit: str = "", project: str = "", stage: str = "new",
    ) -> bool:
        """Stage-aware promotional decision.

        - new/warming: NEVER promotional (100% organic, build trust first)
        - established: 5% promotional max
        - trusted: Use normal organic_ratio
        """
        if stage in ("new", "warming"):
            logger.debug(
                f"Promo decision: sub={subreddit} project={project} "
                f"stage={stage} -> ORGANIC (building trust)"
            )
            return False
        elif stage == "established":
            result = random.random() < 0.05
            logger.debug(
                f"Promo decision: sub={subreddit} project={project} "
                f"stage={stage} -> {'PROMO' if result else 'ORGANIC'} (5% chance)"
            )
            return result
        else:  # trusted or unknown
            result = self._should_be_promotional()
            logger.debug(
                f"Promo decision: sub={subreddit} project={project} "
                f"stage={stage} -> {'PROMO' if result else 'ORGANIC'} "
                f"(ratio={1 - self.organic_ratio:.0%})"
            )
            return result

    # ── Post Type Detection ───────────────────────────────────────────

    def _detect_post_type(self, title: str, body: str) -> str:
        """Detect the type of Reddit post for better response targeting."""
        text = f"{title} {body}".lower()

        # Question post — seeking answers
        question_signals = [
            "?", "how do i", "how to", "what is", "which", "anyone know",
            "can someone", "is there", "does anyone", "what do you",
            "should i", "any recommendations", "any suggestions",
        ]
        if any(sig in text for sig in question_signals):
            return "question"

        # Help/troubleshooting — having a problem
        help_signals = [
            "help", "struggling", "stuck", "doesn't work", "broken",
            "not working", "issue", "problem", "error", "can't figure",
            "troubleshoot", "fix",
        ]
        if any(sig in text for sig in help_signals):
            return "help_wanted"

        # Recommendation seeking — wants suggestions
        rec_signals = [
            "recommend", "looking for", "suggest", "alternative",
            "best tool", "best app", "best software", "what tool",
            "what app", "what do you use",
        ]
        if any(sig in text for sig in rec_signals):
            return "recommendation"

        # Showcase — sharing their work
        showcase_signals = [
            "made this", "just finished", "check out", "my first",
            "feedback", "roast my", "rate my", "what do you think",
            "proud of", "finally done",
        ]
        if any(sig in text for sig in showcase_signals):
            return "showcase"

        # Discussion — general topic
        return "discussion"

    def _get_post_type_instruction(self, post_type: str) -> str:
        """Get specific instructions based on post type."""
        instructions = {
            "question": (
                "This is a QUESTION post — the person needs an answer. "
                "Give a direct, specific answer first, then elaborate briefly. "
                "Don't restate their question back to them."
            ),
            "help_wanted": (
                "This is a HELP post — someone is struggling with a problem. "
                "Be empathetic but get to the solution quickly. "
                "If you've dealt with this before, share what worked."
            ),
            "recommendation": (
                "This is a RECOMMENDATION request — they want tool/software suggestions. "
                "If recommending something, explain WHY it fits their specific use case. "
                "Mention what you've personally used or tested."
            ),
            "showcase": (
                "This is a SHOWCASE post — someone is sharing their work. "
                "Give genuine, specific feedback. Point out what works well. "
                "If you have a constructive tip, frame it positively."
            ),
            "discussion": (
                "This is a DISCUSSION post — share your perspective or experience. "
                "Add a new angle or insight, don't just agree. "
                "Be conversational and engage with the topic."
            ),
        }
        return instructions.get(post_type, instructions["discussion"])

    # ── Subreddit Persona ────────────────────────────────────────────

    def _get_subreddit_persona(self, subreddit: str) -> Dict:
        """Get the persona config for a subreddit."""
        return self.SUBREDDIT_PERSONAS.get(subreddit, self.DEFAULT_PERSONA)

    # ── Expert Persona Context ───────────────────────────────────────

    def _get_expert_context(self, persona_name: str) -> tuple:
        """Get persona instruction and domain knowledge for an expert persona.

        Returns (persona_instruction: str, domain_knowledge: str)
        """
        if not self.EXPERT_PERSONAS:
            self.load_expert_personas()

        persona = self.EXPERT_PERSONAS.get(persona_name, {})
        if not persona:
            return ("Be helpful and knowledgeable.", "")

        # Pick a random tone variant
        tone_variants = persona.get("tone_variants", ["Be helpful and specific."])
        tone = random.choice(tone_variants)

        # Build domain knowledge string
        knowledge = persona.get("domain_knowledge", {})
        tools_raw = knowledge.get("tools", [])
        concepts = knowledge.get("concepts", [])
        jargon = knowledge.get("jargon", [])

        # Flatten tools dict (categories -> flat list of tool strings)
        flat_tools = []
        if isinstance(tools_raw, dict):
            for _cat, items in tools_raw.items():
                if isinstance(items, list):
                    flat_tools.extend(items)
        elif isinstance(tools_raw, list):
            flat_tools = tools_raw
        # Pick a random subset to keep prompt size reasonable
        if len(flat_tools) > 15:
            flat_tools = random.sample(flat_tools, 15)

        domain_parts = []
        if flat_tools:
            domain_parts.append(f"Tools you know: {', '.join(flat_tools)}")
        if concepts:
            domain_parts.append(f"Concepts you master: {', '.join(concepts[:10])}")
        if jargon:
            domain_parts.append(f"Use this jargon naturally: {', '.join(jargon[:10])}")

        # Add reply rules
        rules = persona.get("reply_rules", [])
        if rules:
            domain_parts.append("Expert reply style: " + "; ".join(rules[:5]))

        domain_str = "\n".join(domain_parts)

        persona_instruction = f"{persona.get('description', '')} {tone}"

        return (persona_instruction, domain_str)

    # ── Business Context ──────────────────────────────────────────────

    def _build_business_context(self, project: Dict) -> str:
        """Build rich business context string from project config."""
        proj = project.get("project", project)
        profile = proj.get("business_profile", {})

        if not profile:
            name = proj.get("name", "")
            url = proj.get("url", "")
            desc = proj.get("description", "")
            if not name:
                return ""
            return f"Product: {name} ({url})\nDescription: {desc}"

        lines = []
        lines.append("=== PRODUCT KNOWLEDGE (use ONLY these facts) ===")
        lines.append(f"Name: {proj.get('name', '')}")
        lines.append(f"URL: {proj.get('url', '')}")
        if proj.get("tagline"):
            lines.append(f"Tagline: {proj['tagline']}")
        if proj.get("type"):
            lines.append(f"Type: {proj['type']}")
        lines.append(f"Description: {proj.get('description', '')}")

        selling_points = proj.get("selling_points", [])
        if selling_points:
            lines.append("\nKey Selling Points:")
            for sp in selling_points:
                lines.append(f"  - {sp}")

        features = profile.get("features", [])
        if features:
            lines.append("\nFeatures:")
            for feat in features:
                lines.append(f"  - {feat['name']}: {feat['description']}")

        pricing = profile.get("pricing", {})
        if pricing:
            lines.append(f"\nPricing: {pricing.get('model', 'unknown')}")
            if pricing.get("free_tier"):
                lines.append(f"  Free tier: {pricing['free_tier']}")
            for plan in pricing.get("paid_plans", []):
                lines.append(
                    f"  {plan['name']}: {plan['price']} — "
                    f"{plan.get('highlights', '')}"
                )

        socials = profile.get("socials", {})
        active_socials = {k: v for k, v in socials.items() if v}
        if active_socials:
            lines.append("\nOfficial Links:")
            for platform_name, link in active_socials.items():
                lines.append(f"  {platform_name}: {link}")

        faqs = profile.get("faqs", [])
        if faqs:
            lines.append("\nFAQs:")
            for faq in faqs:
                lines.append(f"  Q: {faq['q']}")
                lines.append(f"  A: {faq['a']}")

        competitors = profile.get("competitors", [])
        if competitors:
            lines.append("\nCompetitor Awareness:")
            for comp in competitors:
                lines.append(
                    f"  vs {comp['name']}: {comp['differentiation']}"
                )

        rules = profile.get("rules", {})
        never_say = rules.get("never_say", [])
        always_accurate = rules.get("always_accurate", [])
        if never_say or always_accurate:
            lines.append("\nCRITICAL RULES:")
            for phrase in never_say:
                lines.append(f"  NEVER say: \"{phrase}\"")
            for rule in always_accurate:
                lines.append(f"  ALWAYS: {rule}")

        lines.append("=== END PRODUCT KNOWLEDGE ===")
        return "\n".join(lines)

    def _get_business_context_for_prompt(
        self, project: Dict, is_promotional: bool
    ) -> str:
        """Full context for promotional posts, empty for organic."""
        if not is_promotional:
            return ""
        return self._build_business_context(project)

    # ── Promotional Instruction ───────────────────────────────────────

    _MENTION_PATTERNS = [
        "If it naturally fits, you may briefly mention {name} as something you've personally tried.",
        "You can casually reference {name} only if it directly solves the person's specific problem.",
        "You may mention {name} in passing — like 'I switched to {name} a while back'.",
        "Only mention {name} if they explicitly ask for recommendations. Otherwise just be helpful.",
        "You may bring up {name} as one option among several: 'stuff like X, Y, or {name}'.",
        "If the topic is exactly about what {name} does, you can mention it naturally. Otherwise just help.",
    ]

    def _get_promotional_instruction(
        self, project: Dict, is_promotional: bool
    ) -> str:
        """Build the promotional instruction with varied mention patterns.

        Strict separation: organic replies MUST NOT mention any product.
        """
        proj = project.get("project", project)
        name = proj.get("name", "")

        if not is_promotional:
            # Hard block on product mentions in organic mode
            return (
                f"ABSOLUTE RULE: Do NOT mention {name} or any specific product, "
                f"tool, service, or brand by name. Do NOT link to any website. "
                f"Do NOT hint at a product ('there's this tool I use...'). "
                f"Just be genuinely helpful based on general experience. "
                f"Your reply must contain ZERO product references."
            )

        pattern = random.choice(self._MENTION_PATTERNS).format(name=name)
        return (
            f"{pattern} "
            f"Don't force it — if it doesn't fit organically, just be helpful "
            f"without mentioning it."
        )

    def _get_tone_instruction(self, style: str) -> str:
        """Pick a random tone variation for the given style."""
        variations = self.TONE_VARIATIONS.get(
            style, self.TONE_VARIATIONS["helpful_casual"]
        )
        return random.choice(variations)

    # ── Generation Methods ────────────────────────────────────────────

    def generate_reddit_comment(
        self,
        post_title: str,
        post_body: str,
        subreddit: str,
        project: Dict,
        is_promotional: Optional[bool] = None,
    ) -> str:
        """Generate a Reddit comment for a given post."""
        if is_promotional is None:
            is_promotional = self._should_be_promotional()

        promo_instruction = self._get_promotional_instruction(
            project, is_promotional
        )
        business_context = self._get_business_context_for_prompt(
            project, is_promotional
        )

        # Detect post type for targeted response
        post_type = self._detect_post_type(post_title, post_body)
        post_type_instruction = self._get_post_type_instruction(post_type)

        # Get subreddit-specific persona
        persona = self._get_subreddit_persona(subreddit)
        tone_style = persona["tone"]
        if not is_promotional:
            tone_style = "organic_engagement"

        # A/B test override for tone
        proj_name = project.get("project", {}).get("name", "")
        if self._ab_engine and proj_name:
            try:
                exp_id, variant, value = self._ab_engine.get_variant(proj_name, "tone")
                if variant and value:
                    tone_style = value
                    # Store experiment info for result recording
                    self._last_ab_experiment = (exp_id, variant)
            except Exception:
                pass

        tone_instruction = self._get_tone_instruction(tone_style)

        # Gather research context and failure rules
        research_context = getattr(self, "_research_context", "") or ""
        failure_rules = getattr(self, "_failure_rules", "") or ""

        template = self.templates.get("reddit_comment", "")
        if not template:
            template = (
                "Write a helpful Reddit comment for a post in r/{subreddit}.\n"
                "Title: {post_title}\n"
                "Content: {post_body}\n"
                "{post_type_instruction}\n"
                "{promotional_instruction}\n"
                "{business_context}"
            )

        prompt = template.format(
            subreddit=subreddit,
            post_title=post_title,
            post_body=post_body[:500],
            promotional_instruction=promo_instruction,
            business_context=business_context,
            post_type_instruction=post_type_instruction,
            subreddit_persona=persona["persona"],
            research_context=research_context,
            failure_avoidance=failure_rules,
        )

        system_prompt = (
            f"You are a Reddit user in r/{subreddit}. {tone_instruction} "
            f"Write only the comment text, nothing else. No meta-text."
        )

        return self.llm.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            task="creative",
        )

    def generate_reddit_post(
        self,
        subreddit: str,
        topic: str,
        project: Dict,
        is_promotional: Optional[bool] = None,
    ) -> Dict[str, str]:
        """Generate a Reddit post (title + body)."""
        if is_promotional is None:
            is_promotional = self._should_be_promotional()

        promo_instruction = self._get_promotional_instruction(
            project, is_promotional
        )
        business_context = self._get_business_context_for_prompt(
            project, is_promotional
        )

        template = self.templates.get("reddit_post", "")
        if not template:
            template = (
                "Write a valuable post for r/{subreddit} about {topic}.\n"
                "{promotional_instruction}"
            )

        research_context = getattr(self, "_research_context", "") or ""
        prompt = template.format(
            subreddit=subreddit,
            topic=topic,
            promotional_instruction=promo_instruction,
            business_context=business_context,
            research_context=research_context,
        )

        persona = self._get_subreddit_persona(subreddit)
        tone_instruction = self._get_tone_instruction(persona["tone"])

        system_prompt = (
            f"You are an active member of r/{subreddit}. {tone_instruction} "
            f"Output format: first line 'TITLE: ...' then 'BODY: ...'"
        )

        result = self.llm.generate(prompt=prompt, system_prompt=system_prompt, task="creative")

        title = ""
        body = result
        if "TITLE:" in result and "BODY:" in result:
            parts = result.split("BODY:", 1)
            title = parts[0].replace("TITLE:", "").strip()
            body = parts[1].strip()
        elif "\n" in result:
            lines = result.split("\n", 1)
            title = lines[0].strip()
            body = lines[1].strip()

        return {"title": title, "body": body}

    def generate_twitter_tweet(
        self,
        context: str,
        project: Dict,
        persona: str = "tech_enthusiast",
        is_promotional: Optional[bool] = None,
    ) -> str:
        """Generate a tweet."""
        if is_promotional is None:
            is_promotional = self._should_be_promotional()

        promo_instruction = self._get_promotional_instruction(
            project, is_promotional
        )
        business_context = self._get_business_context_for_prompt(
            project, is_promotional
        )

        template = self.templates.get("twitter_tweet", "")
        if not template:
            template = (
                "Write a tweet about {topic}.\n"
                "{promotional_instruction}"
            )

        topic = project.get("project", {}).get("description", "tech")

        prompt = template.format(
            persona=persona,
            topic=topic,
            context=context,
            promotional_instruction=promo_instruction,
            business_context=business_context,
        )

        system_prompt = (
            f"You are a {persona} on Twitter. "
            f"Write ONLY the tweet text. Maximum 280 characters. "
            f"No quotes, no labels."
        )

        tweet = self.llm.generate(prompt=prompt, system_prompt=system_prompt, task="creative")

        if len(tweet) > 280:
            tweet = tweet[:277] + "..."

        return tweet

    def generate_twitter_reply(
        self,
        tweet_text: str,
        tweet_author: str,
        project: Dict,
        persona: str = "tech_enthusiast",
        is_promotional: Optional[bool] = None,
        tweet_meta: Optional[Dict] = None,
    ) -> str:
        """Generate a Twitter reply.

        Args:
            tweet_meta: Optional dict with engagement context:
                followers, favorite_count, retweet_count, reply_count
        """
        if is_promotional is None:
            is_promotional = self._should_be_promotional()

        promo_instruction = self._get_promotional_instruction(
            project, is_promotional
        )
        business_context = self._get_business_context_for_prompt(
            project, is_promotional
        )

        # Build enriched author context for better reply targeting
        enriched_tweet = tweet_text
        if tweet_meta:
            parts = []
            followers = tweet_meta.get("followers", 0)
            if followers:
                parts.append(f"{followers:,} followers")
            favs = tweet_meta.get("favorite_count", 0)
            if favs:
                parts.append(f"{favs} likes")
            rts = tweet_meta.get("retweet_count", 0)
            if rts:
                parts.append(f"{rts} RTs")
            if parts:
                enriched_tweet = f"{tweet_text}\n[Author: {', '.join(parts)}]"

        # Get expert context
        persona_instruction, domain_knowledge = self._get_expert_context(persona)

        template = self.templates.get("twitter_reply", "")
        if not template:
            template = (
                "Reply to @{tweet_author}'s tweet:\n"
                "{tweet_text}\n"
                "{promotional_instruction}"
            )

        prompt = template.format(
            persona=persona,
            tweet_author=tweet_author,
            tweet_text=enriched_tweet,
            promotional_instruction=promo_instruction,
            business_context=business_context,
            persona_instruction=persona_instruction,
            domain_knowledge=domain_knowledge,
        )

        system_prompt = (
            f"{persona_instruction} "
            f"You are a {persona} replying on Twitter. "
            f"Write ONLY the reply text. Maximum 280 characters. "
            f"No quotes, no labels."
        )

        reply = self.llm.generate(prompt=prompt, system_prompt=system_prompt, task="creative")

        if len(reply) > 280:
            reply = reply[:277] + "..."

        return reply

    def generate_telegram_reply(
        self,
        message_text: str,
        group_name: str,
        project: Dict,
        author_name: str = "",
        persona: str = "helpful_casual",
        is_promotional: Optional[bool] = None,
    ) -> str:
        """Generate a Telegram group reply."""
        if is_promotional is None:
            is_promotional = self._should_be_promotional()

        promo_instruction = self._get_promotional_instruction(
            project, is_promotional
        )
        business_context = self._get_business_context_for_prompt(
            project, is_promotional
        )

        post_type = self._detect_post_type(message_text, "")
        post_type_instruction = self._get_post_type_instruction(post_type)

        # Get expert context
        persona_instruction, domain_knowledge = self._get_expert_context(persona)

        template = self.templates.get("telegram_reply", "")
        if not template:
            template = (
                "Reply to this Telegram message in {group_name}:\n"
                "{persona_instruction}\n"
                "{domain_knowledge}\n"
                "From @{author_name}: {message_text}\n"
                "{post_type_instruction}\n"
                "{promotional_instruction}\n"
                "{business_context}"
            )

        prompt = template.format(
            group_name=group_name,
            author_name=author_name or "someone",
            message_text=message_text[:800],
            post_type_instruction=post_type_instruction,
            promotional_instruction=promo_instruction,
            business_context=business_context,
            persona_instruction=persona_instruction,
            domain_knowledge=domain_knowledge,
        )

        tone_instruction = self._get_tone_instruction(persona)

        system_prompt = (
            f"{persona_instruction} "
            f"You are an expert member of a Telegram group. {tone_instruction} "
            f"Write ONLY the reply text. Keep it under 500 characters. "
            f"Be conversational and natural. No quotes, no labels, no meta-text."
        )

        reply = self.llm.generate(
            prompt=prompt, system_prompt=system_prompt, task="creative"
        )

        # Trim if too long (Telegram limit is 4096 but keep it readable)
        if len(reply) > 2000:
            reply = reply[:1997] + "..."

        return reply

    # ── User Post System ─────────────────────────────────────────────

    # Post types weighted by trust stage — new accounts only get safe types
    POST_TYPE_WEIGHTS = {
        "new":         {"tip": 0.5, "question": 0.5},
        "warming":     {"tip": 0.3, "question": 0.4, "tutorial": 0.3},
        "established": {"tip": 0.15, "question": 0.2, "tutorial": 0.2,
                        "experience": 0.2, "discovery": 0.15, "trend_react": 0.1},
        "trusted":     {"tip": 0.1, "question": 0.1, "tutorial": 0.15,
                        "experience": 0.15, "discovery": 0.15, "comparison": 0.15,
                        "before_after": 0.1, "trend_react": 0.1},
    }

    def select_post_type(
        self, stage: str, learned_weights: Dict[str, float] = None,
    ) -> str:
        """Pick a post type via weighted random selection for the given stage.

        If learned_weights are provided (from learning engine), use those
        instead of the static defaults. Can be called with self=None for
        static-like usage when learned_weights are provided.
        """
        default_weights = {
            "new": {"question": 0.5, "tip": 0.3, "discussion": 0.2},
            "growing": {"tip": 0.4, "discussion": 0.3, "question": 0.2, "story": 0.1},
            "established": {"tip": 0.3, "discussion": 0.3, "story": 0.2, "question": 0.2},
            "trusted": {"tip": 0.25, "discussion": 0.25, "story": 0.25, "question": 0.25},
        }
        if self is not None:
            stage_weights = self.POST_TYPE_WEIGHTS
        else:
            stage_weights = default_weights
        weights = learned_weights or stage_weights.get(
            stage, stage_weights["new"]
        )
        types = list(weights.keys())
        probs = list(weights.values())
        return random.choices(types, weights=probs, k=1)[0]

    def generate_user_post(
        self,
        subreddit: str,
        project: Dict,
        post_type: str = "tip",
        is_promotional: bool = False,
        trend_context: str = "",
        target_length: str = "",
    ) -> Dict[str, str]:
        """Generate a diverse Reddit post simulating real user behavior.

        Returns: {"title": ..., "body": ..., "post_type": post_type}
        """
        promo_instruction = self._get_promotional_instruction(
            project, is_promotional
        )
        business_context = self._get_business_context_for_prompt(
            project, is_promotional
        )
        research_context = getattr(self, "_research_context", "") or ""

        # Build topic from project keywords/description
        proj = project.get("project", project)
        reddit_cfg = project.get("reddit", {})
        keywords = reddit_cfg.get("keywords", [])
        topic = proj.get("description", "")
        if keywords:
            topic = f"{topic} (keywords: {', '.join(keywords[:5])})"

        # Load evolved template if available, else file-based, else generic
        template_name = f"reddit_user_{post_type}"
        template = None
        proj_name = proj.get("name", "")
        if self._db and proj_name:
            try:
                template = self._db.get_evolved_prompt(proj_name, template_name)
            except Exception:
                pass
        if not template:
            template = self.templates.get(template_name, "")
        if not template:
            template = self.templates.get("reddit_post", "")
        if not template:
            template = (
                "Write a {post_type} post for r/{subreddit} about {topic}.\n"
                "{promotional_instruction}\n{business_context}"
            )

        prompt = template.format(
            subreddit=subreddit,
            topic=topic,
            promotional_instruction=promo_instruction,
            business_context=business_context,
            research_context=research_context,
            trend_context=trend_context or "(no specific trend)",
            post_type=post_type,
        )

        persona = self._get_subreddit_persona(subreddit)
        tone_instruction = self._get_tone_instruction(persona["tone"])

        length_hint = ""
        if target_length == "short":
            length_hint = "Be very concise: 50-100 words max. "
        elif target_length == "long":
            length_hint = "Write a thorough, detailed response: 200-400 words. "

        system_prompt = (
            f"You are a real user of r/{subreddit}. {tone_instruction} "
            f"{length_hint}"
            f"Write a {post_type} post. "
            f"Output format: 'TITLE: ...' then 'BODY: ...'"
        )

        result = self.llm.generate(
            prompt=prompt, system_prompt=system_prompt, task="creative"
        )

        # Parse TITLE/BODY
        title = ""
        body = result
        if "TITLE:" in result and "BODY:" in result:
            parts = result.split("BODY:", 1)
            title = parts[0].replace("TITLE:", "").strip()
            body = parts[1].strip()
        elif "\n" in result:
            lines = result.split("\n", 1)
            title = lines[0].strip()
            body = lines[1].strip()

        return {"title": title, "body": body, "post_type": post_type}

    def generate_user_tweet(
        self,
        project: Dict,
        tweet_type: str = "tip",
        is_promotional: bool = False,
        trend_context: str = "",
        reddit_url: str = "",
    ) -> str:
        """Generate a tweet simulating real user behavior.

        Can include a reddit_url for cross-platform sharing.
        """
        promo_instruction = self._get_promotional_instruction(
            project, is_promotional
        )
        business_context = self._get_business_context_for_prompt(
            project, is_promotional
        )

        proj = project.get("project", project)
        topic = proj.get("description", "tech")

        reddit_context = ""
        if reddit_url:
            reddit_context = f"Link to include: {reddit_url}"

        template = self.templates.get("twitter_user_post", "")
        if not template:
            template = (
                "Write a {tweet_type} tweet about {topic}.\n"
                "{promotional_instruction}\n{reddit_context}"
            )

        persona_cfg = self._get_subreddit_persona("")  # default persona
        persona = persona_cfg.get("tone", "tech_enthusiast")

        prompt = template.format(
            persona=persona,
            topic=topic,
            tweet_type=tweet_type,
            promotional_instruction=promo_instruction,
            business_context=business_context,
            reddit_context=reddit_context,
        )

        system_prompt = (
            f"You are a {persona} on Twitter. "
            f"Write ONLY the tweet text. Maximum 280 characters. "
            f"No quotes, no labels, no meta-text."
        )

        tweet = self.llm.generate(
            prompt=prompt, system_prompt=system_prompt, task="creative"
        )

        if len(tweet) > 280:
            tweet = tweet[:277] + "..."

        return tweet
