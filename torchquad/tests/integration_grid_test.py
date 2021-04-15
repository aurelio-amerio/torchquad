import sys

sys.path.append("../")

from integration.integration_grid import IntegrationGrid
import torch

torch.set_printoptions(10)


def test_integration_grid():
    """Tests the integration grid in integration.integration_grid for illegal values"""

    # Generate a grid in different dimensions with different N on different domains
    eps = 2e-8  # error bound

    # test 1: N is float, 1-D
    N = 10.0
    integration_domain = [[0, 1]]
    grid = IntegrationGrid(N, integration_domain)

    # test if number of points is correct
    assert grid._N == N
    assert len(grid.points) == N
    for dim in range(len(integration_domain)):
        # test if mesh width is correct
        assert torch.abs(grid.h[dim] - 1 / (N - 1)) < eps
        # test if all points are inside
        assert torch.all(grid.points[:, dim] >= integration_domain[dim][0])
        assert torch.all(grid.points[:, dim] <= integration_domain[dim][1])
    # print("1-D (float) test: N =", N, ", grid_N =", grid._N, ", error:", torch.abs(grid.h[dim] - 1 / (N - 1)))

    # test 2: N is int, 3-D
    N = 4 ** 3
    integration_domain = [[0, 2], [-2, 1], [0.5, 1]]
    grid = IntegrationGrid(N, integration_domain)

    # test if number of points is correct
    assert grid._N == int(N ** (1 / len(integration_domain)) + 1e-8)
    assert len(grid.points) == N
    for dim in range(len(integration_domain)):
        domain_width = integration_domain[dim][1] - integration_domain[dim][0]
        # test if mesh width is correct
        assert torch.abs(grid.h[dim] - domain_width / (grid._N - 1)) < eps
        # test if all points are inside
        assert torch.all(grid.points[:, dim] >= integration_domain[dim][0])
        assert torch.all(grid.points[:, dim] <= integration_domain[dim][1])
    # print("3-D (int) test: N =", N, ", grid_N = N^(1/3) =", grid._N, ", error:", torch.abs(grid.h[dim] - domain_width / (grid._N - 1)))

    # test 3: N is float, 3-D
    N = 4.0 ** 3
    integration_domain = [[0, 2], [-2, 1], [0.5, 1]]
    grid = IntegrationGrid(N, integration_domain)

    # test if number of points is correct
    assert grid._N == int(N ** (1 / len(integration_domain)) + 1e-8)
    assert len(grid.points) == N
    for dim in range(len(integration_domain)):
        domain_width = integration_domain[dim][1] - integration_domain[dim][0]
        # test if mesh width is correct
        assert torch.abs(grid.h[dim] - domain_width / (grid._N - 1)) < eps
        # test if all points are inside
        assert torch.all(grid.points[:, dim] >= integration_domain[dim][0])
        assert torch.all(grid.points[:, dim] <= integration_domain[dim][1])
    # print("3-D (float) test: N =", N, ", grid_N = N^(1/3) =", grid._N, ", error:", torch.abs(grid.h[dim] - domain_width / (grid._N - 1)))


test_integration_grid()
