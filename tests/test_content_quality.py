from app import content_quality as cq

UPVOTERSS = (
    "Reading time: 4 min | Post permalink For years we have been working on server software. "
    + "Real article text. " * 30
    + "Top comments Great post! Comment permalink I disagree. Comment permalink Nice. Comment permalink"
)


def test_upvoterss_body_keeps_post_and_top_comments():
    body = cq.clean_body(UPVOTERSS)
    assert body.startswith("For years")
    assert "permalink" not in body.lower()
    post, comments = cq.split_post(body)
    assert post.startswith("For years") and "Top comments" not in post
    assert comments == "Top comments:\n- Great post!\n- I disagree.\n- Nice."
    assert cq.classify(UPVOTERSS) == "broken"
    assert cq.classify(body) == "ok"


def test_comment_count_is_capped(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "SOCIAL_MAX_COMMENTS", 2)
    _, comments = cq.split_post(cq.clean_body(UPVOTERSS))
    assert comments.count("\n- ") == 2


def test_image_post_keeps_comments_only():
    body = cq.clean_body("Post permalink Top comments lol Comment permalink")
    assert body == "Top comments:\n- lol"
    assert cq.split_post(body) == ("", body)
    assert cq.classify("") == "empty"


def test_embedding_text_ignores_comments():
    from app.clustering import embed_text_for

    body = cq.clean_body(UPVOTERSS)
    assert "disagree" not in embed_text_for({"title": "Deno", "content_raw": body})
    image_post = cq.clean_body("Post permalink Top comments " + "long comment text " * 40 + "Comment permalink")
    assert embed_text_for({"title": "Funny picture", "content_raw": image_post}) == "Funny picture"


def test_binary_payload_is_dropped():
    garbage = "".join(chr(c) for c in range(1, 31)) * 20
    assert cq.is_binary(garbage)
    assert cq.clean_body(garbage) == ""
    assert cq.classify(garbage) == "broken"


def test_regular_text_untouched_and_capped():
    text = "Normal article. " * 10
    assert cq.clean_body(text) == text.strip()
    assert len(cq.clean_body("x" * (cq.MAX_BODY_CHARS + 50))) == cq.MAX_BODY_CHARS
    assert cq.classify("short") == "thin"
    assert cq.classify(None) == "empty"


def test_single_chrome_line_is_not_broken():
    assert cq.classify("Access denied, said the minister. " + "Regular reporting text. " * 20) == "ok"
