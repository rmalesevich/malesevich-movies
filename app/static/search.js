/* TMDB autocomplete: type a title, pick a result, the pick is saved.
   Deliberately dependency-free so the NAS never needs to reach a CDN. */
(function () {
  "use strict";

  var DEBOUNCE_MS = 250;
  var MIN_CHARS = 2;

  function debounce(fn, wait) {
    var timer;
    return function () {
      var args = arguments, self = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(self, args); }, wait);
    };
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function setup(form) {
    var input = form.querySelector(".tmdb-search");
    var hidden = form.querySelector(".tmdb-id");
    var results = form.querySelector(".results");
    if (!input || !hidden || !results) return;

    var items = [];
    var activeIndex = -1;
    var controller = null;

    function clear() {
      results.innerHTML = "";
      items = [];
      activeIndex = -1;
    }

    function choose(movie) {
      hidden.value = movie.id;
      input.value = movie.title + (movie.year ? " (" + movie.year + ")" : "");
      input.disabled = true;
      clear();
      form.submit();
    }

    function highlight(index) {
      var nodes = results.querySelectorAll(".result");
      for (var i = 0; i < nodes.length; i++) {
        nodes[i].classList.toggle("active", i === index);
      }
      activeIndex = index;
      if (nodes[index]) nodes[index].scrollIntoView({ block: "nearest" });
    }

    function render(movies) {
      clear();
      items = movies;
      movies.forEach(function (movie, index) {
        var row = el("div", "result");
        if (movie.poster_url) {
          var img = el("img");
          img.src = movie.poster_url;
          img.alt = "";
          img.loading = "lazy";
          row.appendChild(img);
        } else {
          row.appendChild(el("div", "noimg"));
        }
        var box = el("div");
        box.appendChild(el("div", "r-title", movie.title));
        var bits = [];
        if (movie.year) bits.push(movie.year);
        if (movie.overview) bits.push(movie.overview.slice(0, 90) + "…");
        box.appendChild(el("div", "r-meta", bits.join(" · ")));
        row.appendChild(box);
        row.addEventListener("mousedown", function (event) {
          event.preventDefault();
          choose(movie);
        });
        row.addEventListener("mouseenter", function () { highlight(index); });
        results.appendChild(row);
      });
    }

    var search = debounce(function (query) {
      if (controller) controller.abort();
      controller = new AbortController();
      fetch("/api/tmdb/search?q=" + encodeURIComponent(query), {
        signal: controller.signal,
        headers: { Accept: "application/json" }
      })
        .then(function (response) {
          if (!response.ok) throw new Error("search failed: " + response.status);
          return response.json();
        })
        .then(function (data) { render(data.results || []); })
        .catch(function (error) {
          if (error.name === "AbortError") return;
          clear();
          var row = el("div", "result");
          row.appendChild(el("div", "r-meta", "Search failed — " + error.message));
          results.appendChild(row);
        });
    }, DEBOUNCE_MS);

    input.addEventListener("input", function () {
      var query = input.value.trim();
      if (query.length < MIN_CHARS) { clear(); return; }
      search(query);
    });

    input.addEventListener("keydown", function (event) {
      if (!items.length) {
        if (event.key === "Enter") event.preventDefault();
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        highlight((activeIndex + 1) % items.length);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        highlight((activeIndex - 1 + items.length) % items.length);
      } else if (event.key === "Enter") {
        event.preventDefault();
        choose(items[activeIndex >= 0 ? activeIndex : 0]);
      } else if (event.key === "Escape") {
        clear();
      }
    });

    input.addEventListener("blur", function () {
      setTimeout(clear, 120);
    });

    // Enter is handled above; never submit an empty pick.
    form.addEventListener("submit", function (event) {
      if (!hidden.value) event.preventDefault();
    });
  }

  document.querySelectorAll(".search-form").forEach(setup);
})();
