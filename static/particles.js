(function() {
    // Respect prefers-reduced-motion setting
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        return;
    }

    // Check if canvas already exists
    if (document.getElementById('meow-particles-canvas')) return;

    function initParticles() {
        if (!document.body) return;

        const canvas = document.createElement('canvas');
        canvas.id = 'meow-particles-canvas';
        canvas.style.position = 'fixed';
        canvas.style.top = '0';
        canvas.style.left = '0';
        canvas.style.width = '100vw';
        canvas.style.height = '100vh';
        canvas.style.pointerEvents = 'none';
        canvas.style.zIndex = '0';
        document.body.prepend(canvas);

        const ctx = canvas.getContext('2d');
        let width, height;

        function resize() {
            width = window.innerWidth;
            height = window.innerHeight;
            canvas.width = width;
            canvas.height = height;
        }

        window.addEventListener('resize', resize);
        resize();

        const colors = [
            'rgba(88, 101, 242, 0.65)',
            'rgba(139, 92, 246, 0.6)',
            'rgba(236, 72, 153, 0.55)',
            'rgba(56, 189, 248, 0.5)'
        ];

        const particles = [];
        const numParticles = 45;

        class Particle {
            constructor() {
                this.reset(true);
            }

            reset(initial = false) {
                this.x = Math.random() * width;
                this.y = initial ? Math.random() * height : height + 15;
                this.size = Math.random() * 2.5 + 1.5;
                this.speedY = -(Math.random() * 0.6 + 0.2);
                this.speedX = (Math.random() - 0.5) * 0.4;
                this.color = colors[Math.floor(Math.random() * colors.length)];
                this.isPaw = Math.random() > 0.75;
                this.opacity = Math.random() * 0.5 + 0.3;
            }

            update() {
                this.y += this.speedY;
                this.x += this.speedX;

                if (this.y < -20 || this.x < -20 || this.x > width + 20) {
                    this.reset();
                }
            }

            draw() {
                ctx.globalAlpha = this.opacity;
                ctx.fillStyle = this.color;

                if (this.isPaw) {
                    const s = this.size * 1.8;
                    // Main pad
                    ctx.beginPath();
                    ctx.arc(this.x, this.y, s, 0, Math.PI * 2);
                    ctx.fill();
                    // 3 cute toe pads
                    ctx.beginPath();
                    ctx.arc(this.x - s * 1.1, this.y - s * 1.1, s * 0.5, 0, Math.PI * 2);
                    ctx.arc(this.x, this.y - s * 1.5, s * 0.55, 0, Math.PI * 2);
                    ctx.arc(this.x + s * 1.1, this.y - s * 1.1, s * 0.5, 0, Math.PI * 2);
                    ctx.fill();
                } else {
                    ctx.beginPath();
                    ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
                    ctx.fill();
                }
            }
        }

        for (let i = 0; i < numParticles; i++) {
            particles.push(new Particle());
        }

        function animate() {
            ctx.clearRect(0, 0, width, height);

            particles.forEach(p => {
                p.update();
                p.draw();
            });

            requestAnimationFrame(animate);
        }

        animate();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initParticles);
    } else {
        initParticles();
    }
})();
