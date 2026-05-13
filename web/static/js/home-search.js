(function () {
  const input = document.getElementById("home-search-input");
  if (!input) return;

  const empty = document.getElementById("home-search-empty");
  const groups = Array.from(document.querySelectorAll("[data-home-search-group]"));
  const items = Array.from(document.querySelectorAll("[data-search-item]"));
  if (items.length === 0) return;

  function normalize(value) {
    return (value || "").toString().toLocaleLowerCase("bg-BG").trim();
  }

  function applyFilter() {
    const query = normalize(input.value);
    let visibleTotal = 0;

    groups.forEach((group) => {
      const groupItems = group.querySelectorAll("[data-search-item]");
      let visibleInGroup = 0;
      groupItems.forEach((item) => {
        const text = normalize(item.dataset.searchText || item.textContent || "");
        const match = query === "" || text.includes(query);
        item.classList.toggle("is-search-hidden", !match);
        if (match) visibleInGroup += 1;
      });
      group.classList.toggle("is-search-empty", visibleInGroup === 0 && query !== "");
      visibleTotal += visibleInGroup;
    });

    if (empty) {
      empty.hidden = !(query !== "" && visibleTotal === 0);
    }
  }

  input.addEventListener("input", applyFilter);
  input.addEventListener("search", applyFilter);
  applyFilter();
})();
