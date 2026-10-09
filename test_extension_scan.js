const assert = require("assert");
const scan = require("./extension/scan.js");

const yt = "https://www.youtube.com/watch?v=-5gV7DH7oQ4";

const pageOnly = scan.collectVideos({
  href: yt,
  title: "Если Вы Разговариваете с кошкой - YouTube",
  videos: [{ currentSrc: "blob:https://www.youtube.com/abc", src: "", sources: [], duration: 2735 }],
  iframes: [],
});
assert.strictEqual(pageOnly.length, 1);
assert.strictEqual(pageOnly[0].url, yt);
assert.strictEqual(pageOnly[0].title, "Если Вы Разговариваете с кошкой");

const article = scan.collectVideos({
  href: "https://example.com/post",
  title: "Статья",
  iframes: [
    { src: "https://www.youtube.com/embed/abcdefghijk", title: "Первый" },
    { src: "https://www.youtube.com/embed/abcdefghijk?start=3", title: "Тот же" },
    { src: "https://vk.com/video_ext.php?oid=-1&id=2&hash=secret", title: "ВК" },
    { src: "https://player.vimeo.com/video/12345", title: "" },
  ],
  videos: [
    {
      currentSrc: "https://cdn.example.com/thumb/preview.jpg",
      sources: ["https://cdn.example.com/a/clip.mp4", "https://cdn.example.com/a/clip.webm"],
      title: "",
      duration: 40,
    },
    {
      currentSrc: "https://cdn.example.com/ads/spot.mp4",
      sources: [],
      duration: 1.2,
    },
  ],
});
assert.deepStrictEqual(article.map((item) => item.url), [
  "https://www.youtube.com/watch?v=abcdefghijk",
  "https://vk.com/video_ext.php?oid=-1&id=2&hash=secret",
  "https://vimeo.com/12345",
  "https://cdn.example.com/a/clip.mp4",
]);
assert.strictEqual(article[0].title, "Первый");
assert.strictEqual(article[2].title, "Vimeo");

const home = scan.collectVideos({
  href: "https://www.youtube.com/feed/subscriptions",
  title: "YouTube",
  iframes: [],
  videos: [],
  links: [{ href: "https://www.youtube.com/watch?v=zzzzzzzzzzz", title: "Рекомендация" }],
  textUrls: ["https://www.youtube.com/watch?v=yyyyyyyyyyy"],
});
assert.deepStrictEqual(home, []);

const watchKeepsPage = scan.collectVideos({
  href: yt,
  title: "Ролик - YouTube",
  links: [{ href: "https://www.youtube.com/watch?v=zzzzzzzzzzz", title: "Рядом" }],
  textUrls: ["https://www.youtube.com/watch?v=yyyyyyyyyyy"],
});
assert.strictEqual(watchKeepsPage.length, 1);
assert.strictEqual(watchKeepsPage[0].url, yt);

const fromLink = scan.collectVideos({
  href: "https://example.com/post",
  title: "Подборка",
  links: [
    { href: "https://www.youtube.com/watch?v=abcdefghijk", title: "Лекция" },
    { href: "https://example.com/about", title: "О сайте" },
  ],
  textUrls: ["https://rutube.ru/video/deadbeef12/"],
});
assert.deepStrictEqual(fromLink.map((item) => item.url), [
  "https://www.youtube.com/watch?v=abcdefghijk",
  "https://rutube.ru/video/deadbeef12/",
]);
assert.strictEqual(fromLink[0].title, "Лекция");

const vkPage = scan.collectVideos({
  href: "https://vk.com/video-1_2",
  title: "Клип",
});
assert.strictEqual(vkPage.length, 1);
assert.strictEqual(vkPage[0].url, "https://vk.ru/video-1_2");

const meta = scan.collectVideos({
  href: "https://news.example/story",
  title: "Сюжет дня",
  metas: ["https://www.youtube.com/watch?v=zzzzzzzzzzz"],
});
assert.strictEqual(meta.length, 1);
assert.strictEqual(meta[0].url, "https://www.youtube.com/watch?v=zzzzzzzzzzz");
assert.strictEqual(meta[0].title, "Сюжет дня");

const gcPayload = Buffer.from(
  '{"video_hash":"e0d48990424815b4cca708573e6510bf","user_id":-1}',
).toString("base64");
const gcPlayer =
  "https://vh-api-1-de.gceuproxy.com/sign-player/?json=" + gcPayload + "&s=abc123";
const gcCopy = gcPlayer.replace("s=abc123", "s=def456");
const gc = scan.collectVideos({
  href: "https://vasilinfo.ru/vkshopszap",
  title: "",
  iframes: [
    { src: gcPlayer, title: "" },
    { src: gcCopy, title: "" },
  ],
});
assert.strictEqual(gc.length, 1);
assert.strictEqual(gc[0].url, gcPlayer);
assert.strictEqual(gc[0].title, "GetCourse");

const pruffme = scan.collectVideos({
  href: "https://pruffme.com/landing/u1/tmp1",
  title: "Страница",
  mediaJson:
    '{"name":"Вебинар про сайты","url":null,"path":"user/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/video/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/video.mp4"}',
});
assert.strictEqual(pruffme.length, 1);
assert.strictEqual(
  pruffme[0].url,
  "https://video.pruffme.com/user/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/video/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/video.mp4"
);
assert.strictEqual(pruffme[0].title, "Вебинар про сайты");

console.log("extension scan ok");
