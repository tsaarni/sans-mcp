def pytest_addoption(parser):
    parser.addoption(
        "--update-api",
        action="store_true",
        default=False,
        help="Update the public API surface snapshot in etc/api_surface.txt",
    )
