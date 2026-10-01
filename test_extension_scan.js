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
});
assert.deepStrictEqual(home, []);

const meta = scan.collectVideos({
  href: "https://news.example/story",
  title: "Сюжет дня",
  metas: ["https://www.youtube.com/watch?v=zzzzzzzzzzz"],
});
assert.strictEqual(meta.length, 1);
assert.strictEqual(meta[0].url, "https://www.youtube.com/watch?v=zzzzzzzzzzz");
assert.strictEqual(meta[0].title, "Сюжет дня");

console.log("extension scan ok");
