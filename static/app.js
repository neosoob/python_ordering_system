const yuan = new Intl.NumberFormat("zh-CN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function formatCurrency(value) {
  return `¥${yuan.format(Number(value || 0))}`;
}

function createFlash(message, category = "success") {
  let stack = document.querySelector("[data-flash-stack]");
  if (!stack) {
    stack = document.createElement("section");
    stack.className = "flash-stack";
    stack.setAttribute("data-flash-stack", "");
    const main = document.querySelector(".main-shell");
    if (main) {
      main.prepend(stack);
    }
  }

  const item = document.createElement("div");
  item.className = `flash flash-${category}`;
  item.textContent = message;
  stack.prepend(item);

  window.setTimeout(() => {
    item.remove();
    if (!stack.children.length) {
      stack.remove();
    }
  }, 2200);
}

function updateCartNav(cart) {
  const nav = document.querySelector("[data-cart-nav]");
  if (!nav) {
    return;
  }
  nav.textContent = cart.count ? `购物车 (${cart.count})` : "购物车";
}

function renderIndexCartSummary(cart) {
  const panel = document.querySelector("[data-cart-summary]");
  if (!panel) {
    return;
  }

  if (!cart.items.length) {
    panel.innerHTML = `
      <div class="section-header">
        <div><h2>购物车</h2></div>
        <a class="text-link" href="/cart">展开详情</a>
      </div>
      <div class="empty-state" data-cart-summary-empty>
        <p>暂无菜品</p>
      </div>
    `;
    return;
  }

  const list = cart.items
    .map(
      (item) => `
        <div class="cart-item" data-cart-summary-item="${item.dish_id}">
          <div>
            <strong>${item.name}</strong>
            <p>${item.quantity} 份</p>
          </div>
          <div class="cart-summary-actions">
            <span>${formatCurrency(item.subtotal)}</span>
            <form action="/cart/remove/${item.dish_id}" method="post" data-cart-remove-form>
              <input type="hidden" name="next" value="/">
              <button class="button button-ghost button-small" type="submit">移除</button>
            </form>
          </div>
        </div>
      `
    )
    .join("");

  panel.innerHTML = `
    <div class="section-header">
      <div><h2>购物车</h2></div>
      <a class="text-link" href="/cart">展开详情</a>
    </div>
    <div class="cart-list compact-list" data-cart-summary-list>${list}</div>
    <div class="cart-total" data-cart-summary-total>
      <span>合计</span>
      <strong>${formatCurrency(cart.total)}</strong>
    </div>
    <form class="stack-form" action="/checkout" method="post" data-cart-checkout-form>
      <label>
        取餐人
        <input type="text" name="customer_name" placeholder="例如：张三">
      </label>
      <button class="button button-primary button-block" type="submit">提交订单</button>
    </form>
  `;
}

function renderCartPage(cart) {
  const page = document.querySelector("[data-cart-page]");
  if (!page) {
    return;
  }

  if (!cart.items.length) {
    page.innerHTML = `
      <div class="empty-state tall-empty" data-cart-page-empty>
        <h2>购物车为空</h2>
        <a class="button button-primary" href="/">去点餐</a>
      </div>
    `;
    return;
  }

  const items = cart.items
    .map(
      (item) => `
        <article class="cart-item cart-item-detailed" data-cart-page-item="${item.dish_id}">
          <img class="cart-thumb" src="/static/${item.image_path}" alt="${item.name}">
          <div class="cart-info">
            <h3>${item.name}</h3>
            <p>${item.description}</p>
            <span class="price-text">单价 ${formatCurrency(item.price)}</span>
          </div>
          <form class="inline-form compact-form" action="/cart/update/${item.dish_id}" method="post" data-cart-update-form>
            <input type="hidden" name="next" value="/cart">
            <label>
              数量
              <input type="number" name="quantity" min="0" max="99" value="${item.quantity}">
            </label>
            <button class="button button-secondary" type="submit">更新</button>
          </form>
          <div class="cart-actions">
            <strong data-cart-page-subtotal>${formatCurrency(item.subtotal)}</strong>
            <form action="/cart/remove/${item.dish_id}" method="post" data-cart-remove-form>
              <input type="hidden" name="next" value="/cart">
              <button class="button button-ghost" type="submit">移除</button>
            </form>
          </div>
        </article>
      `
    )
    .join("");

  page.innerHTML = `
    <div class="cart-list" data-cart-page-list>${items}</div>
    <form class="checkout-bar checkout-form" action="/checkout" method="post" data-cart-checkout-form>
      <div class="checkout-summary" data-cart-page-summary>
        <p>共 ${cart.distinct_count} 种菜品</p>
        <strong>${formatCurrency(cart.total)}</strong>
      </div>
      <label class="checkout-name">
        取餐人
        <input type="text" name="customer_name" placeholder="例如：张三">
      </label>
      <button class="button button-primary" type="submit">提交订单</button>
    </form>
  `;
}

function applyCartState(cart) {
  updateCartNav(cart);
  renderIndexCartSummary(cart);
  renderCartPage(cart);
}

async function submitCartForm(form) {
  const response = await fetch(form.action, {
    method: form.method || "POST",
    body: new FormData(form),
    headers: {
      "X-Requested-With": "XMLHttpRequest",
    },
  });

  const data = await response.json();
  if (!response.ok || !data.ok) {
    throw new Error(data.message || "操作失败，请稍后重试。");
  }

  applyCartState(data.cart);
  createFlash(data.message, "success");
}

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (
    !form.matches("[data-cart-add-form]") &&
    !form.matches("[data-cart-update-form]") &&
    !form.matches("[data-cart-remove-form]")
  ) {
    return;
  }

  event.preventDefault();

  const button = form.querySelector("button[type='submit']");
  if (button) {
    button.disabled = true;
  }

  try {
    await submitCartForm(form);
  } catch (error) {
    createFlash(error.message, "warning");
  } finally {
    if (button) {
      button.disabled = false;
    }
  }
});
